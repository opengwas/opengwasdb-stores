"""The parent's exact metadata content-hash algorithm, as a shared helper.

Reproduces ``output_metadata_content_sha256`` from the BAF metadata refresh
audit runner: over the refreshed ``gwas-ssf/*/GCST*/*`` entries, the
``manifests/*.tsv`` and ``refresh-receipt.tsv``, record a symlink's target or a
regular metadata file's ``sha256`` and ``size``, then hash the sorted JSON.

Symlinks are never followed or opened; regular files must be ``.yaml``,
``.tsv`` or ``.json`` (a regular ``.gz``/data file is rejected), so this reads
regular metadata only and never an association body.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Iterable

METADATA_SUFFIXES: tuple[str, ...] = (".yaml", ".tsv", ".json")


class MetadataContentError(ValueError):
    """The metadata tree does not match the auditable content-hash contract."""


def metadata_snapshot(paths: Iterable[Path]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for path in sorted({Path(p) for p in paths}):
        entry = path.lstat()
        if stat.S_ISLNK(entry.st_mode):
            result[str(path)] = {"link": str(path.readlink())}
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise MetadataContentError(f"unexpected non-regular metadata entry: {path}")
        if path.suffix not in METADATA_SUFFIXES:
            raise MetadataContentError(
                f"unexpected regular metadata file type (data bodies are forbidden): {path}"
            )
        result[str(path)] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": entry.st_size,
            "mtime_ns": entry.st_mtime_ns,
        }
    return result


def metadata_content_sha256(root: Path | str) -> tuple[str, int]:
    """Return ``(content_sha256, entry_count)`` for a refreshed metadata root."""
    base = Path(root)
    paths = (
        list((base / "gwas-ssf").glob("*/GCST*/*"))
        + list((base / "manifests").glob("*.tsv"))
        + [base / "refresh-receipt.tsv"]
    )
    snapshot = metadata_snapshot(paths)
    content = {
        path: ({"link": record["link"]} if "link" in record else {"sha256": record["sha256"], "size": record["size"]})
        for path, record in snapshot.items()
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    return digest, len(content)


__all__ = [
    "METADATA_SUFFIXES",
    "MetadataContentError",
    "metadata_content_sha256",
    "metadata_snapshot",
]
