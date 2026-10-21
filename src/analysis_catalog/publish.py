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
import os
import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from analysis_catalog import BUILDER_VERSION
from analysis_catalog.policy import POLICY, Decision

KEEP_VERSIONS = 5
PARTIAL_SUFFIX = ".partial"
BUNDLE_INPUTS = ("release.yaml", "build.yaml", "validation.yaml")
PACKAGE_DIR = Path(__file__).resolve().parent


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


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def compute_version(decisions: Iterable[Decision], registry: Path) -> str:
    """Hash of everything the catalog reads, and the builder that reads it.

    Per registered bundle: its decision and reason, `release.yaml`, `build.yaml`
    and `validation.yaml`; and the Release Manifest of every Store whose
    decision rests on it (listed, placeholder, refused). An excluded Store's
    manifest is not read, so editing one changes nothing; changing an excluded
    Store's decision does change the version.
    """
    digest = hashlib.sha256()
    _feed(digest, "builder_version", BUILDER_VERSION.encode("utf-8"))
    _feed(digest, "policy", "\n".join(POLICY).encode("utf-8"))
    for source in sorted(PACKAGE_DIR.rglob("*.py")):
        _feed(digest, f"source/{source.relative_to(PACKAGE_DIR)}", source.read_bytes())
    for decision in sorted(decisions, key=lambda d: d.store_id):
        root = registry / decision.store_id
        _feed(digest, f"{decision.store_id}/decision", f"{decision.decision}\n{decision.reason}".encode("utf-8"))
        for name in BUNDLE_INPUTS:
            _feed(digest, f"{decision.store_id}/{name}", _read(root / name))
        if decision.read_manifest:
            _feed(digest, f"{decision.store_id}/analyses.tsv", _read(root / "analyses.tsv"))
    return digest.hexdigest()[:16]


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
