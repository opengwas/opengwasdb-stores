"""The record writer: writes or refreshes an LD Store's record from the Store itself.

It writes the record's writer fields (`record.WRITER_FIELDS`) and keeps every
other field exactly as a person wrote it. A published Store is immutable, so
once a record holds a Store's provenance, a rewrite must find the same Store:
the same `kind`, `location`, provenance and provenance digest. Anything else is
another Store, and is refused, naming each field that differs, with the record
left as it was. A changed link (`measured.links`) is refused the same way.
Only `measured.store_bytes` may change, and the writer reports the change. A
field the spec has added since the record was written is
filled in when the digest shows the same Store. A writer value that breaks the
spec, such as a `created_at` that is not a UTC time, is refused the same way,
before anything is written.

The build that made a Store also writes its `rebuild` (#541), when given one:
as given, validated, and never changed silently. A record that already holds a
`rebuild`, whether a person or a build wrote it, keeps it: a different one is
refused, naming each field that differs.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from ld_registry import record
from ld_registry.ids import require_valid_ld_id
from ld_registry.provenance import ld_info, provenance_sha256, record_provenance
from ogstores.store_info import Runner
from ogstores.store_size import store_size_on_disk

HEADER = (
    "# LD Store record (docs/spec/ld-store-registry.md, ADR 0042).\n"
    "# kind, location, provenance, provenance_sha256 and measured are written by\n"
    "# `python3 -m ld_registry write`; a person writes every other field, and the\n"
    "# writer keeps it as written.\n"
)

#: Writer fields that must not change once written: they say which Store it is.
UNCHANGING = ("kind", "location", "provenance_sha256")


class WriterRefusal(ValueError):
    """The writer will not write this record; the message names the record and each field."""


class Written(NamedTuple):
    path: Path
    previous_store_bytes: int | None
    store_bytes: int


def _differences(existing: dict[str, Any], computed: dict[str, Any]) -> list[str]:
    """The fields of `computed` that contradict what `existing` already records.

    Under the same digest the provenance is the same, so a field the record
    lacks, a provenance field or `measured.links`, is one the spec added since
    it was written, not a contradiction.
    """
    differ = [f for f in UNCHANGING if f in existing and existing[f] != computed[f]]
    same_store = existing.get("provenance_sha256") == computed["provenance_sha256"]
    recorded = existing.get("provenance")
    if isinstance(recorded, dict):
        new = computed["provenance"]
        keys = set(recorded) if same_store else set(recorded) | set(new)
        differ += [f"provenance.{key}" for key in sorted(keys) if recorded.get(key) != new.get(key)]
    measured = existing.get("measured")
    # A record written before links were measured gains them under the same digest.
    if isinstance(measured, dict) and not (same_store and "links" not in measured):
        old, new = measured.get("links") or {}, computed["measured"].get("links") or {}
        differ += [f"measured.links.{name}" for name in sorted(set(old) | set(new)) if old.get(name) != new.get(name)]
    return differ


def _raise(error: OSError) -> None:
    raise error


def links(location: Path) -> dict[str, str]:
    """Every link under the Store, by its path in the Store, and its target with
    every link resolved. The walk follows no link; a dangling link is refused,
    and an unreadable directory raises rather than being skipped."""
    found: dict[str, str] = {}
    for root, directories, files in os.walk(location, onerror=_raise):  # never descends a linked directory
        for name in directories + files:
            entry = Path(root) / name
            if not entry.is_symlink():
                continue
            inside = entry.relative_to(location).as_posix()
            try:
                found[inside] = str(entry.resolve(strict=True))
            except (OSError, RuntimeError) as exc:
                raise WriterRefusal(
                    f"{location}: the link {inside} -> {os.readlink(entry)} does not resolve: {exc}"
                ) from exc
    return dict(sorted(found.items()))


def infer_kind(info: dict[str, Any]) -> str:
    """The Store's kind, from its provenance: `ld info` prints no kind.

    A Genotype Store's provenance carries `filesets`, `inputs` and `arrays`; an
    LD Store's carries none of them (genotype-store.md, "Container and shared
    data"). Any of the three without all three is neither kind, and refused.
    """
    markers = ("filesets", "inputs", "arrays")
    present = [key for key in markers if key in info]
    if not present:
        return "ld_store"
    missing = [key for key in markers if key not in info]
    if missing:
        raise WriterRefusal(
            f"the provenance has {' and '.join(present)} but no {' or '.join(missing)}: it is neither a "
            "Genotype Store (filesets, inputs and arrays) nor an LD Store (none of them)"
        )
    return "genotype_store"


def genotype_bytes(location: Path, info: dict[str, Any]) -> int:
    """The bytes of the pgen, psam and pvar files `filesets` pins, by a stat of each.

    A pin's path is relative to the sidecar root, `location`. A malformed
    fileset or pin, an absolute path, a pin that does not resolve, or one whose
    size is not the pin's, is refused, naming the fileset and pin.
    """
    filesets = info["filesets"]
    if not isinstance(filesets, list):
        raise WriterRefusal(f"filesets must be a list, got {type(filesets).__name__}")
    total = 0
    for number, fileset in enumerate(filesets):
        if not isinstance(fileset, dict):
            raise WriterRefusal(f"filesets[{number}] must be a mapping, got {type(fileset).__name__}")
        for kind in ("pgen", "psam", "pvar"):
            where = f"filesets[{number}].{kind}"
            pin = fileset.get(kind)
            if not isinstance(pin, dict):
                raise WriterRefusal(f"{where} is missing or not a mapping: a fileset pins a pgen, psam and pvar")
            for key in ("path", "size"):
                if key not in pin:
                    raise WriterRefusal(f"{where} has no {key}")
            if not isinstance(pin["path"], str) or isinstance(pin["size"], bool) or not isinstance(pin["size"], int):
                raise WriterRefusal(f"{where}: path must be text and size a whole number")
            if Path(pin["path"]).is_absolute():
                raise WriterRefusal(f"{where} ({pin['path']}) is absolute: a pin's path is relative to the sidecar root")
            path = location / pin["path"]
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise WriterRefusal(f"{where} ({path}) does not resolve: {exc.strerror}") from exc
            if size != pin["size"]:
                raise WriterRefusal(f"{where} ({path}) is {size} bytes, but the pin says {pin['size']}")
            total += size
    return total


def _rebuild_differences(existing: Any, given: dict[str, Any]) -> list[str]:
    """The `rebuild` fields in which `given` contradicts the record's own."""
    if not isinstance(existing, dict):
        return [] if existing is None else ["rebuild"]
    return [f"rebuild.{key}" for key in sorted(set(existing) | set(given)) if existing.get(key) != given.get(key)]


def write_record(
    ld_root: Path | str,
    ld_id: str,
    location: Path | str,
    runner: Runner | None = None,
    size: Callable[[Path], int] = store_size_on_disk,
    measure_links: Callable[[Path], dict[str, str]] = links,
    rebuild: dict[str, Any] | None = None,
) -> Written:
    """Write `<ld_root>/<ld_id>/record.yaml` from the LD Store at `location`, and `rebuild` when the build gives it."""
    require_valid_ld_id(ld_id)
    path = record.record_path(Path(ld_root), ld_id)
    existing: dict[str, Any] = {}
    if path.exists() or path.is_symlink():
        document, errors = record.read_document(path)
        if document is None:
            raise WriterRefusal("; ".join(str(error) for error in errors))
        existing = document
    if existing.get("ld_id", ld_id) != ld_id:
        raise WriterRefusal(f"{path}: ld_id: the record says {existing['ld_id']!r}, not {ld_id!r}")

    location = Path(location).absolute()
    info = ld_info(location, runner=runner)
    kind = infer_kind(info)
    linked = measure_links(location)
    computed: dict[str, Any] = {
        "kind": kind,
        "location": str(location),
        "provenance": record_provenance(info),
        "provenance_sha256": provenance_sha256(info),
        "measured": {
            "store_bytes": size(location),
            **({"genotype_bytes": genotype_bytes(location, info)} if kind == "genotype_store" else {}),
            **({"links": linked} if linked else {}),
        },
    }
    differ = _differences(existing, computed)
    if rebuild is not None:
        differ += _rebuild_differences(existing.get("rebuild"), rebuild)
    if differ:
        raise WriterRefusal(
            f"{path}: {', '.join(differ)}: the Store at {location} is not the Store this record describes; "
            "a published Store never changes, so another Store needs a record of its own"
        )

    written = {**existing, "ld_id": ld_id, **{name: computed[name] for name in record.WRITER_FIELDS}}
    if rebuild is not None:
        written["rebuild"] = rebuild
    order = [f.name for f in record.FIELDS if "." not in f.name]
    document = {key: written[key] for key in order if key in written}
    document.update((key, value) for key, value in written.items() if key not in document)

    # A writer value that breaks the spec came from the Store, and a published
    # Store never changes, so no rewrite could mend it: refuse before writing.
    # A field a person has still to write is the CLI's to report afterwards.
    ours = set(record.WRITER_FIELDS) | ({"rebuild"} if rebuild is not None else set())
    malformed = [e for e in record.validate(document, path) if e.field.split(".")[0] in ours]
    if malformed:
        raise WriterRefusal("; ".join(str(error) for error in malformed))

    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    staged.write_text(
        HEADER + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=10**6),
        encoding="utf-8",
    )
    os.replace(staged, path)
    previous = existing.get("measured", {}).get("store_bytes") if isinstance(existing.get("measured"), dict) else None
    return Written(path, previous, computed["measured"]["store_bytes"])
