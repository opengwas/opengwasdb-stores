"""The record writer: writes or refreshes an LD Store's record from the Store itself.

It writes the record's writer fields (`record.WRITER_FIELDS`) and keeps every
other field exactly as a person wrote it. A published Store is immutable, so
once a record holds a Store's provenance, a rewrite must find the same Store:
the same `kind`, `location`, provenance and provenance digest. Anything else is
another Store, and is refused, naming each field that differs, with the record
left as it was. Only `measured.store_bytes` may change, and the writer reports
the change. A writer value that breaks the spec, such as a `created_at` that is
not a UTC time, is refused the same way, before anything is written.
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
    """The fields of `computed` that contradict what `existing` already records."""
    differ = [f for f in UNCHANGING if f in existing and existing[f] != computed[f]]
    recorded = existing.get("provenance")
    if isinstance(recorded, dict):
        new = computed["provenance"]
        differ += [f"provenance.{key}" for key in sorted(set(recorded) | set(new)) if recorded.get(key) != new.get(key)]
    return differ


def write_record(
    ld_root: Path | str,
    ld_id: str,
    location: Path | str,
    runner: Runner | None = None,
    size: Callable[[Path], int] = store_size_on_disk,
) -> Written:
    """Write `<ld_root>/<ld_id>/record.yaml` from the LD Store at `location`."""
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
    computed: dict[str, Any] = {
        "kind": "ld_store",
        "location": str(location),
        "provenance": record_provenance(info),
        "provenance_sha256": provenance_sha256(info),
        "measured": {"store_bytes": size(location)},
    }
    differ = _differences(existing, computed)
    if differ:
        raise WriterRefusal(
            f"{path}: {', '.join(differ)}: the Store at {location} is not the Store this record describes; "
            "a published Store never changes, so another Store needs a record of its own"
        )

    written = {**existing, "ld_id": ld_id, **{name: computed[name] for name in record.WRITER_FIELDS}}
    order = [f.name for f in record.FIELDS if "." not in f.name]
    document = {key: written[key] for key in order if key in written}
    document.update((key, value) for key, value in written.items() if key not in document)

    # A writer value that breaks the spec came from the Store, and a published
    # Store never changes, so no rewrite could mend it: refuse before writing.
    # A field a person has still to write is the CLI's to report afterwards.
    malformed = [e for e in record.validate(document, path) if e.field.split(".")[0] in record.WRITER_FIELDS]
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
