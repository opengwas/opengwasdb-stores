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
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
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

#: The status of a Store the Data API serves, the only one that may be a `default`.
SERVED = "served"

#: The source kind of an imported matrix (`ogld import`), whose description
#: is a JSON object of documented fields (ogld build-inputs.md §12).
IMPORTED_MATRIX = "ld_matrix"

PERSON = "person"
WRITER = "writer"
#: The `rebuild` section's writer: the build that made the Store, through the
#: writer's `rebuild` option (#541), or a person for a Store built before the registry.
BUILD_OR_PERSON = "build or person"

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


def _true(value: Any) -> str | None:
    return None if value is True else f"{value!r} is not true: say `default: true`, or leave the field out"


def _absolute_path(value: Any) -> str | None:
    return _text(value) or (None if Path(value).is_absolute() else f"{value!r} is not an absolute path")


def _mapping(value: Any) -> str | None:
    return None if isinstance(value, Mapping) else f"must be a mapping, got {type(value).__name__}"


def _commands(value: Any) -> str | None:
    if not isinstance(value, list) or not value or any(_text(command) for command in value):
        return "must be a list of one or more commands"
    return None


def _inputs(value: Any) -> str | None:
    problem = ("must be a mapping of each input's name to its {path, sha256}, each path absolute and each "
               "sha256 lower-case hex")
    if not isinstance(value, Mapping) or not value:
        return problem
    for entry in value.values():
        if (not isinstance(entry, Mapping) or set(entry) != {"path", "sha256"}
                or _absolute_path(entry["path"]) or _sha256(entry["sha256"])):
            return problem
    return None


def _text_or_mapping(value: Any) -> str | None:
    return None if isinstance(value, Mapping) else _text(value)


def _links(value: Any) -> str | None:
    if not isinstance(value, Mapping) or not value:
        return "must be a mapping of every link in the Store, by its path in the Store, to its resolved target"
    bad = [str(name) for name, target in value.items() if _absolute_path(target)]
    return f"{', '.join(bad)}: each target must be an absolute path" if bad else None


def _unrecorded_targets(value: Any) -> str | None:
    problem = "must be a list of one or more {path, reason}, each path absolute and each reason non-empty text"
    if not isinstance(value, list) or not value:
        return problem
    for entry in value:
        if (not isinstance(entry, Mapping) or set(entry) != {"path", "reason"}
                or _absolute_path(entry["path"]) or _text(entry["reason"])):
            return problem
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
    # The Store a consumer takes for its ancestry (#537, ogdb-api#195).
    Field("default", PERSON, False, _true),
    Field("used_for", PERSON, True, _text),
    Field("issue", PERSON, False, _text),
    Field("notes", PERSON, False, _text),
    # Targets of the Store's links that no record describes, each with why (#538).
    Field("unrecorded_targets", PERSON, False, _unrecorded_targets),
    # Written by the build that made the Store (the writer's `rebuild` option,
    # #541), or by a person for a Store built before the registry.
    Field("rebuild", BUILD_OR_PERSON, True, _mapping),
    Field("rebuild.repository", BUILD_OR_PERSON, True, _text),
    Field("rebuild.version", BUILD_OR_PERSON, True, _text),
    Field("rebuild.script", BUILD_OR_PERSON, True, _text),
    Field("rebuild.commands", BUILD_OR_PERSON, True, _commands),
    # The files the commands read, each its path and the digest the Store's `source.sha256`
    # holds of it, so a rebuild names its inputs as the Store pinned them (#542).
    Field("rebuild.inputs", BUILD_OR_PERSON, False, _inputs),
    Field("location", WRITER, True, _absolute_path),
    Field("provenance", WRITER, True, _mapping),
    Field("provenance.source_kind", WRITER, True, _text),
    Field("provenance.source_description", WRITER, True, _text_or_mapping),
    # An imported matrix's (`IMPORTED_MATRIX`) description is a JSON object,
    # recorded as a mapping of these fields (ogld build-inputs.md §12); any
    # other kind's is text (`_import_errors`).
    Field("provenance.source_description.url", WRITER, True, _text),
    Field("provenance.source_description.licence", WRITER, True, _text),
    Field("provenance.source_description.producer_commit", WRITER, True, _text),
    Field("provenance.source_description.transformation", WRITER, True, _text),
    Field("provenance.source_description.liftover_chain", WRITER, True, _text),
    Field("provenance.source_description.qc", WRITER, True, _text),
    Field("provenance.source_description.far_bp_by_chromosome", WRITER, True, _mapping),
    Field("provenance.source_description.far_target_bp", WRITER, False, _whole),
    Field("provenance.source_description.displaced_cap", WRITER, False, _whole),
    Field("provenance.source_description.displaced_by_chromosome", WRITER, False, _mapping),
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
    Field("provenance.variants_dropped", WRITER, False, _mapping),
    Field("provenance.software", WRITER, True, _mapping),
    Field("provenance.software.ogld", WRITER, True, _text),
    Field("provenance.created_at", WRITER, True, _utc_time),
    Field("provenance.wall_seconds", WRITER, True, _number),
    Field("provenance_sha256", WRITER, True, _sha256),
    Field("measured", WRITER, True, _mapping),
    Field("measured.store_bytes", WRITER, True, _whole),
    Field("measured.links", WRITER, False, _links),
    Field("measured.genotype_bytes", WRITER, False, _whole),
)

#: Mappings whose keys are the engine's, copied as it records them.
OPEN_MAPPINGS = frozenset({
    "provenance.counts",
    "provenance.variants_dropped",
    "provenance.software",
    "provenance.source_description.far_bp_by_chromosome",
    "provenance.source_description.displaced_by_chromosome",
    "measured.links",
    "rebuild.inputs",
})

#: The top-level fields the record writer writes; a person writes the rest.
WRITER_FIELDS: tuple[str, ...] = tuple(f.name for f in FIELDS if f.written_by == WRITER and "." not in f.name)


def record_path(ld_root: Path, ld_id: str) -> Path:
    return Path(ld_root) / ld_id / RECORD_FILENAME


def lookup(document: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    """Whether `document` holds the dotted path `dotted` (such as `source.sha256`), and its value there.

    The one walker of a dotted path: a record's fields, and `opengwasdb ld info`'s
    provenance (`ld_registry.provenance`, `recipe`, `compare`). A step that is
    not a mapping, or a key that is missing, is `(False, None)`.
    """
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


def _import_errors(document: Mapping[str, Any], path: Path) -> list[RecordError]:
    """An imported matrix's description is its object, and its licence is the record's;
    any other kind's description is text (#538)."""
    provenance = document.get("provenance")
    if not isinstance(provenance, Mapping) or "source_description" not in provenance:
        return []  # their own errors say it
    description = provenance["source_description"]
    if provenance.get("source_kind") != IMPORTED_MATRIX:
        if isinstance(description, Mapping):
            return [RecordError(path, "provenance.source_description",
                                f"is an object, but only an {IMPORTED_MATRIX} source's description is one; "
                                f"a {provenance.get('source_kind')!r} source's is text")]
        return []
    if not isinstance(description, Mapping):
        return [RecordError(path, "provenance.source_description",
                            f"must be the {IMPORTED_MATRIX} source's description object, not text")]
    if "licence" in description and document.get("licence") != description["licence"]:
        return [RecordError(path, "licence", (
            f"{document.get('licence')!r} is not the imported matrix's own licence, "
            f"provenance.source_description.licence {description['licence']!r}"))]
    return []


def validate(document: Mapping[str, Any], path: Path) -> list[RecordError]:
    """Every way `document`, read from `path`, breaks the spec; empty when it is valid."""
    errors: list[RecordError] = []
    declared = {f.name for f in FIELDS}
    for field in FIELDS:
        parent = field.name.rpartition(".")[0]
        if parent and not isinstance(lookup(document, parent)[1], Mapping):
            continue  # the parent's own error says it
        present, value = lookup(document, field.name)
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
    errors.extend(_import_errors(document, path))
    for key in document:
        if key not in declared:
            errors.append(RecordError(path, str(key), "is not a record field"))
    if document.get("default") is True and document.get("status") in STATUSES and document["status"] != SERVED:
        errors.append(RecordError(path, "default", f"only a {SERVED} record may be the default, not {document['status']!r}"))
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


class InvalidRecords(ValueError):
    """The registry holds a record that breaks the spec."""

    def __init__(self, errors: list[RecordError]) -> None:
        self.errors = errors
        super().__init__("the LD Store Registry has invalid records:\n" + "\n".join(f"  {e}" for e in errors))


def default_errors(records: list[dict[str, Any]], ld_root: Path) -> list[RecordError]:
    """An error on each `default: true` after the first for one ancestry, naming the record it duplicates."""
    declared: dict[Any, str] = {}
    errors: list[RecordError] = []
    for document in records:
        if document.get("default") is not True or document.get("status") != SERVED:
            continue
        ancestry = (document.get("provenance") or {}).get("ancestry")
        first = declared.setdefault(ancestry, str(document["ld_id"]))
        if first != document["ld_id"]:
            errors.append(RecordError(
                record_path(ld_root, str(document["ld_id"])), "default",
                f"{first} is already the default for ancestry {ancestry}; at most one served record may be",
            ))
    return errors


#: The error on an entry of `ld_root` that is no record's directory.
_NOT_A_RECORD_DIRECTORY = "stores/ld/ holds only 'OGL-\\d{5}' directories"


def load_records(ld_root: Path, *, invalid: bool = False) -> tuple[list[dict[str, Any]], list[RecordError]]:
    """Every valid record under `ld_root`, in id order, and every error among them.

    `ld_root` holds only directories named by an id, each holding its record:
    anything else there, a file or a misnamed directory, is an error. With
    `invalid`, every record read is returned, valid or not; `unread` names the
    errors that left a record unread.
    """
    records: list[dict[str, Any]] = []
    valid: list[dict[str, Any]] = []
    errors: list[RecordError] = []
    if not Path(ld_root).is_dir():
        return records, errors
    for entry in sorted(Path(ld_root).iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not is_valid_ld_id(entry.name):
            errors.append(RecordError(entry, "ld_id", _NOT_A_RECORD_DIRECTORY))
            continue
        document, found = load_record(record_path(Path(ld_root), entry.name))
        errors.extend(found)
        if document is not None and not found:
            valid.append(document)
        if document is not None and (invalid or not found):
            records.append(document)
    errors.extend(default_errors(valid, Path(ld_root)))
    return records, errors


def unread(errors: Sequence[RecordError]) -> list[RecordError]:
    """The errors among `load_records`'s that left a record unread: a `record.yaml` that is missing,
    a link, unreadable or not a mapping, and an entry of `ld_root` that is no record's directory."""
    return [error for error in errors if error.field == RECORD_FILENAME or (
        error.field == "ld_id" and error.message == _NOT_A_RECORD_DIRECTORY)]


def _under(target: str, directory: str) -> bool:
    target_path, directory_path = PurePosixPath(target), PurePosixPath(directory)
    return target_path == directory_path or directory_path in target_path.parents


def link_errors(ld_root: Path, records: Sequence[Mapping[str, Any]]) -> list[RecordError]:
    """Each link (`measured.links`) whose target lies under no record's location,
    or under a `retired` one's, unless the record lists it in `unrecorded_targets`."""
    errors: list[RecordError] = []
    for document in records:
        measured = document.get("measured") or {}
        declared = [entry["path"] for entry in document.get("unrecorded_targets") or []]
        for name, target in sorted((measured.get("links") or {}).items()):
            if any(_under(target, path) for path in declared):
                continue
            holders = [other for other in records
                       if other is not document and _under(target, other["location"])]
            live = [other for other in holders if other["status"] != "retired"]
            if live:
                continue
            if holders:
                problem = f"its target {target} lies under {', '.join(h['ld_id'] for h in holders)}, which is retired"
            else:
                problem = (f"its target {target} lies under no record's location: record that Store, "
                           "or list the target in unrecorded_targets with its reason")
            errors.append(RecordError(record_path(ld_root, document["ld_id"]), f"measured.links.{name}", problem))
    return errors


def valid_records(ld_root: Path | str) -> list[dict[str, Any]]:
    """Every record under `ld_root`, or `InvalidRecords` naming each error: the one loader that refuses.

    A link whose target no live record holds is an error too, checked against the valid records alone.
    The index and the Analysis Catalog's build both read the registry through it.
    """
    records, errors = load_records(Path(ld_root))
    errors += link_errors(Path(ld_root), records)
    if errors:
        raise InvalidRecords(errors)
    return records
