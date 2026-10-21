"""Versions of the catalog under `<artifact-root>/catalog/` (ADR 0039).

```
catalog/
  versions/<version>/      never modified once renamed into place
  versions/<version>.partial/   a build in progress
  current -> versions/<version>
```

A version is a hash of every input the catalog reads plus the builder's own
source, so an unchanged rebuild finds its version already there and writes
nothing. A new version is written under `.partial`, renamed, and only then does
`current` move, atomically, so no reader sees a half-written catalog.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ogstores import bundle as release_bundle

from analysis_catalog import BUILDER_VERSION, summary
from analysis_catalog.policy import POLICY

KEEP_VERSIONS = 5
PARTIAL_SUFFIX = ".partial"
PACKAGE_DIR = Path(__file__).resolve().parent
OGSTORES_DIR = Path(release_bundle.__file__).resolve().parent
# Read in pieces, so hashing a manifest never holds more than this of it.
CHUNK_SIZE = 1 << 20


def catalog_dir(root: Path | str) -> Path:
    """`<artifact-root>/catalog`: not a valid `OGS-` id, so it cannot be a Store's directory."""
    return Path(root) / "catalog"


def versions_dir(root: Path | str) -> Path:
    return catalog_dir(root) / "versions"


def _feed(digest: "hashlib._Hash", label: str, content: bytes | None) -> None:
    # Length-prefixed, so no two sets of inputs hash alike by concatenation.
    name = label.encode("utf-8")
    body = b"\0absent" if content is None else b"\1" + content
    digest.update(len(name).to_bytes(8, "big") + name + len(body).to_bytes(8, "big") + body)


def _feed_file(digest: "hashlib._Hash", label: str, path: Path) -> None:
    """Hash a file as `_feed` would, reading it in `CHUNK_SIZE` pieces.

    A symlink is hashed by its target, so a dangling one hashes as absent.
    """
    name = label.encode("utf-8")
    if not path.is_file():
        _feed(digest, label, None)
        return
    digest.update(len(name).to_bytes(8, "big") + name + (1 + path.stat().st_size).to_bytes(8, "big") + b"\1")
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)


def opengwasdb_identity() -> str:
    """The installed opengwasdb: its distribution version and, when the install records one, its commit."""
    distribution = importlib.metadata.distribution("opengwasdb")
    identity = f"opengwasdb {distribution.version}"
    direct_url = distribution.read_text("direct_url.json")
    if direct_url:
        commit = json.loads(direct_url).get("vcs_info", {}).get("commit_id")
        if commit:
            identity += f" commit {commit}"
    return identity


def _source_files(package: Path) -> list[Path]:
    return sorted(p for p in package.rglob("*.py") if "__pycache__" not in p.parts)


def compute_version(registry: Path, store_ids: Iterable[str]) -> str:
    """Hash of everything a decision depends on, and the builder that reads it.

    Decisions are a pure function of: the registered store ids; every file of
    every registered Release Bundle directory (`check()` reads them all, so any
    can change a decision); the source of `analysis_catalog` (which includes the
    serving policy) and of `ogstores` (`check()`, `Bundle`,
    `partition_membership`); the installed opengwasdb, whose reader parses the
    manifest; and the summary template, which shapes `summary.html`. They are not
    hashed themselves, so no manifest row is read to know the version. The build
    time and the registry commit are left out.
    """
    digest = hashlib.sha256()
    ids = sorted(store_ids)
    _feed(digest, "builder_version", BUILDER_VERSION.encode("utf-8"))
    _feed(digest, "policy", "\n".join(POLICY).encode("utf-8"))
    _feed(digest, "opengwasdb", opengwasdb_identity().encode("utf-8"))
    _feed(digest, "store_ids", "\n".join(ids).encode("utf-8"))
    for name, package in (("analysis_catalog", PACKAGE_DIR), ("ogstores", OGSTORES_DIR)):
        for source in _source_files(package):
            _feed_file(digest, f"source/{name}/{source.relative_to(package)}", source)
    _feed_file(digest, "summary_template", summary.SOURCE)
    for store_id in ids:
        root = registry / store_id
        for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
            _feed_file(digest, f"{store_id}/{path.relative_to(root)}", path)
    return digest.hexdigest()[:16]


def fsync_path(path: Path) -> None:
    """fsync a file or a directory."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(directory: Path) -> None:
    """fsync every file under `directory`, then `directory` itself."""
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            fsync_path(path)
    fsync_path(directory)


def current_target(root: Path | str) -> str | None:
    """The version `current` names, or None when it is missing or not a symlink."""
    link = catalog_dir(root) / "current"
    if not link.is_symlink():
        return None
    return os.path.basename(os.readlink(link))


def swap_current(root: Path | str, version: str) -> None:
    """Point `current` at a version atomically: a temporary symlink, then `os.replace`."""
    catalog = catalog_dir(root)
    temporary = catalog / f".current.{os.getpid()}.tmp"
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    os.symlink(f"versions/{version}", temporary)
    os.replace(temporary, catalog / "current")
    fsync_path(catalog)


def _built_at(version_dir: Path) -> str:
    try:
        connection = sqlite3.connect(f"file:{version_dir / 'catalog.sqlite'}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT value FROM catalog_meta WHERE key = 'built_at'").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return ""
    return row[0] if row and row[0] else ""


def prune(root: Path | str, keep: int = KEEP_VERSIONS) -> list[str]:
    """Remove all but the `keep` most recent versions, never the one `current` names."""
    versions = versions_dir(root)
    names = [p for p in versions.iterdir() if p.is_dir() and not p.name.endswith(PARTIAL_SUFFIX)]
    newest_first = sorted(names, key=lambda p: (_built_at(p), p.name), reverse=True)
    protected = current_target(root)
    removed = []
    for path in newest_first[keep:]:
        if path.name == protected:
            continue
        shutil.rmtree(path)
        removed.append(path.name)
    return removed
