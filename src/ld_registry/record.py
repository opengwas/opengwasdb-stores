"""The LD Store record: its fields, and their validation (docs/spec/ld-store-registry.md).

`stores/ld/<ld_id>/record.yaml` describes one LD Store. Each field has one
writer: a person (`label`, `status`, `licence`, ...), or the record writer
(`kind`, `location`, `provenance` and its digest from `opengwasdb ld info`,
`measured` from a stat of the Store's files). `FIELDS` is the table; the
writer writes exactly its `WRITER` fields, and the spec's field table is its
prose.

Validation returns every error as data, as `ogstores.bundle.check` does, and
never raises for record content. Each error names the record and the field.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from ld_registry.ids import is_valid_ld_id

RECORD_FILENAME = "record.yaml"
#: The registry's records, relative to the repository root.
LD_ROOT = Path("stores", "ld")

#: What a record describes: an LD Store, or a Genotype Store (#539). The LD
#: panels (#540) are rows from their own `resource.yaml`, not records here.
KINDS: tuple[str, ...] = ("ld_store", "genotype_store")

#: An LD Store's status. A record is written from an existing Store's
#: provenance, so a Store still building has none.
STATUSES: tuple[str, ...] = ("served", "built", "retiring", "reference", "retired")

PERSON = "person"
WRITER = "writer"

Check = Callable[[Any], "str | None"]


class Field(NamedTuple):
    name: str
    written_by: str
    required: bool
    check: Check


class RecordError(NamedTuple):
    path: Path
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.field}: {self.message}"


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "must be non-empty text"
    return None


def _one_of(allowed: tuple[str, ...]) -> Check:
    def check(value: Any) -> str | None:
        return None if value in allowed else f"{value!r} is not one of {', '.join(allowed)}"
    return check


def _whole(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return f"{value!r} is not a whole number"
    return None


def _number(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{value!r} is not a number"
    return None


def _absolute_path(value: Any) -> str | None:
    return _text(value) or (None if Path(value).is_absolute() else f"{value!r} is not an absolute path")


def _mapping(value: Any) -> str | None:
    return None if isinstance(value, Mapping) else f"must be a mapping, got {type(value).__name__}"


def _commands(value: Any) -> str | None:
    if not isinstance(value, list) or not value or any(_text(command) for command in value):
        return "must be a list of one or more commands"
    return None


def _ld_id(value: Any) -> str | None:
    return None if is_valid_ld_id(value) else f"{value!r} does not match 'OGL-\\d{{5}}' (ADR 0042)"


def _utc_time(value: Any) -> str | None:
    problem = f"{value!r} is not an ISO 8601 UTC time, such as 2026-11-13T04:27:31Z"
    if not isinstance(value, str) or "T" not in value:
        return problem
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return problem
    return None if moment.utcoffset() == timedelta(0) else problem


def _sha256(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        return f"{value!r} is not a sha256 in lower-case hex"
    return None


#: Every field a record may hold, in the order the writer writes them. A
#: mapping's own keys are declared beneath it, except in `OPEN_MAPPINGS`.
FIELDS: tuple[Field, ...] = (
    Field("ld_id", PERSON, True, _ld_id),
    Field("kind", WRITER, True, _one_of(KINDS)),
    Field("label", PERSON, True, _text),
    Field("status", PERSON, True, _one_of(STATUSES)),
    Field("licence", PERSON, True, _text),
    Field("used_for", PERSON, True, _text),
    Field("issue", PERSON, False, _text),
    Field("notes", PERSON, False, _text),
    # Written by the build that made the Store, or by a person for a Store
    # built before the registry; #541 adds the writer's option for it.
    Field("rebuild", PERSON, True, _mapping),
    Field("rebuild.repository", PERSON, True, _text),
    Field("rebuild.version", PERSON, True, _text),
    Field("rebuild.script", PERSON, True, _text),
    Field("rebuild.commands", PERSON, True, _commands),
    Field("location", WRITER, True, _absolute_path),
    Field("provenance", WRITER, True, _mapping),
    Field("provenance.source_kind", WRITER, True, _text),
    Field("provenance.source_description", WRITER, True, _text),
    Field("provenance.source_panel", WRITER, True, _text),
    Field("provenance.ancestry", WRITER, True, _text),
    Field("provenance.n", WRITER, True, _whole),
    Field("provenance.k", WRITER, True, _whole),
    Field("provenance.reference_assembly", WRITER, True, _text),
    Field("provenance.format_version", WRITER, True, _text),
    Field("provenance.r_scale", WRITER, True, _whole),
    Field("provenance.near_bp", WRITER, True, _whole),
    Field("provenance.far_bp", WRITER, True, _whole),
    Field("provenance.t_far", WRITER, True, _number),
    Field("provenance.t_partner", WRITER, False, _number),
    Field("provenance.n_variants", WRITER, True, _whole),
    Field("provenance.counts", WRITER, False, _mapping),
    Field("provenance.software", WRITER, True, _mapping),
    Field("provenance.software.ogld", WRITER, True, _text),
    Field("provenance.created_at", WRITER, True, _utc_time),
    Field("provenance.wall_seconds", WRITER, True, _number),
    Field("provenance_sha256", WRITER, True, _sha256),
    Field("measured", WRITER, True, _mapping),
    Field("measured.store_bytes", WRITER, True, _whole),
    Field("measured.genotype_bytes", WRITER, False, _whole),
)

#: Mappings whose keys are the engine's, copied as it records them.
OPEN_MAPPINGS = frozenset({"provenance.counts", "provenance.software"})

#: The top-level fields the record writer writes; a person writes the rest.
WRITER_FIELDS: tuple[str, ...] = tuple(f.name for f in FIELDS if f.written_by == WRITER and "." not in f.name)


def record_path(ld_root: Path, ld_id: str) -> Path:
    return Path(ld_root) / ld_id / RECORD_FILENAME


def _lookup(document: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    value: Any = document
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _genotype_bytes_errors(document: Mapping[str, Any], path: Path) -> list[RecordError]:
    """`measured.genotype_bytes` belongs to a Genotype Store, and to no other kind."""
    measured = document.get("measured")
    if not isinstance(measured, Mapping) or document.get("kind") not in KINDS:
        return []  # their own errors say it
    held = measured.get("genotype_bytes") is not None
    if document["kind"] == "genotype_store" and not held:
        return [RecordError(path, "measured.genotype_bytes", "is required for a genotype_store")]
    if document["kind"] != "genotype_store" and held:
        return [RecordError(path, "measured.genotype_bytes", f"is a Genotype Store's; a {document['kind']} has none")]
    return []


def validate(document: Mapping[str, Any], path: Path) -> list[RecordError]:
    """Every way `document`, read from `path`, breaks the spec; empty when it is valid."""
    errors: list[RecordError] = []
    declared = {f.name for f in FIELDS}
    for field in FIELDS:
        parent = field.name.rpartition(".")[0]
        if parent and not isinstance(_lookup(document, parent)[1], Mapping):
            continue  # the parent's own error says it
        present, value = _lookup(document, field.name)
        if not present or value is None:
            if field.required:
                errors.append(RecordError(path, field.name, "is required"))
            continue
        problem = field.check(value)
        if problem:
            errors.append(RecordError(path, field.name, problem))
        if isinstance(value, Mapping) and field.name not in OPEN_MAPPINGS:
            for key in value:
                if f"{field.name}.{key}" not in declared:
                    errors.append(RecordError(path, f"{field.name}.{key}", "is not a record field"))
    errors.extend(_genotype_bytes_errors(document, path))
    for key in document:
        if key not in declared:
            errors.append(RecordError(path, str(key), "is not a record field"))
    if is_valid_ld_id(document.get("ld_id")) and document["ld_id"] != path.parent.name:
        errors.append(RecordError(path, "ld_id", f"{document['ld_id']!r} is not its directory, {path.parent.name!r}"))
    return errors


def read_document(path: Path) -> tuple[dict[str, Any] | None, list[RecordError]]:
    """Read a record's YAML mapping; a read or shape failure is an error, as data.

    Follows `ogstores.bundle._read_yaml` and `_document_errors`.
    """
    def error(message: str) -> tuple[None, list[RecordError]]:
        return None, [RecordError(path, RECORD_FILENAME, message)]

    if path.is_symlink():
        return error("must not be a symbolic link")
    if not path.is_file():
        return error("is missing")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return error(f"is malformed YAML: {exc}")
    except (OSError, UnicodeError) as exc:
        return error(f"could not be read: {type(exc).__name__}: {exc}")
    if not isinstance(document, dict) or not document:
        return error(f"must be a non-empty mapping, got {type(document).__name__}")
    return document, []


def load_record(path: Path) -> tuple[dict[str, Any] | None, list[RecordError]]:
    """Read and validate one record. The document is returned even when it has errors."""
    document, errors = read_document(Path(path))
    if document is None:
        return None, errors
    return document, validate(document, Path(path))


def load_records(ld_root: Path) -> tuple[list[dict[str, Any]], list[RecordError]]:
    """Every record under `ld_root`, in id order, and every error among them.

    `ld_root` holds only directories named by an id, each holding its record:
    anything else there, a file or a misnamed directory, is an error.
    """
    records: list[dict[str, Any]] = []
    errors: list[RecordError] = []
    if not Path(ld_root).is_dir():
        return records, errors
    for entry in sorted(Path(ld_root).iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not is_valid_ld_id(entry.name):
            errors.append(RecordError(entry, "ld_id", "stores/ld/ holds only 'OGL-\\d{5}' directories"))
            continue
        document, found = load_record(record_path(Path(ld_root), entry.name))
        errors.extend(found)
        if document is not None and not found:
            records.append(document)
    return records, errors
