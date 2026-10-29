"""The catalog-history collector: every Analysis Catalog version, read-only (#389).

The root is `<artifact root>/catalog/versions/` unless one is passed. Each
version's `catalog.sqlite` is opened through a `mode=ro` URI and read with three
queries; nothing is written. The total of Analyses is `count(*)` on
`listed_analyses`: the `counts` rows are per dimension and overlap, so a total
is never derived from them.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

from ogstores import paths

from project_site.collect.common import CollectError


def default_root() -> Path:
    return paths.artifact_root() / "catalog" / "versions"


def _read_only(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{urllib.parse.quote(str(database))}?mode=ro", uri=True)


def _changes(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()[1:]  # id <TAB> change
    return dict(Counter(line.split("\t")[1] for line in lines if line))


def _selection(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every Store Release's decision and reason, as the version's `selection` table holds them."""
    return [
        {"store_id": store_id, "decision": decision, "reason": reason}
        for store_id, decision, reason in db.execute(
            "SELECT store_id, decision, reason FROM selection ORDER BY store_id"
        )
    ]


def _version(directory: Path, current: str | None) -> dict[str, Any]:
    database = directory / "catalog.sqlite"
    if not database.is_file():
        raise CollectError(f"{directory.name} has no catalog.sqlite")
    db = _read_only(database)
    try:
        meta = dict(db.execute("SELECT key, value FROM catalog_meta ORDER BY rowid"))
        counts = [
            {"dimension": d, "value": v, "n_analyses": n}
            for d, v, n in db.execute("SELECT dimension, value, n_analyses FROM counts ORDER BY rowid")
        ]
        analyses, stores = db.execute("SELECT count(*), count(DISTINCT store_id) FROM listed_analyses").fetchone()
        # The decisions are read for the version `current` names, which is the one
        # the site's Store pages show; a historical version's are not needed (#391).
        selection = _selection(db) if directory.name == current else None
    finally:
        db.close()
    return {
        "version": directory.name,
        "current": directory.name == current,
        "catalog_meta": meta,
        "counts": counts,
        "totals": {"analyses": analyses, "stores": stores},
        "changes": _changes(directory / "changes.tsv"),
        "selection": selection,
    }


def collect(root: Path | None = None) -> dict[str, Any]:
    root = default_root() if root is None else Path(root)
    if not root.is_dir():
        raise CollectError(f"catalog root {root} does not exist")
    link = root.parent / "current"
    current = link.resolve().name if link.is_symlink() else None
    versions = [_version(d, current) for d in sorted(root.iterdir()) if d.is_dir()]
    versions.sort(key=lambda v: v["catalog_meta"].get("built_at") or "")
    return {"root": str(root), "current": current, "versions": versions}
