"""A Store's size on disk: the one definition (#502).

Engine-free, so `register` and `ogstores.backfill_observed` record it without
importing the engine, and `ogstores.audit._store_bytes` will call it once
opengwas/opengwasdb-stores#498 has landed its changes to `audit.py`.
"""

from __future__ import annotations

from pathlib import Path


def store_size_on_disk(store_dir: Path) -> int:
    """The bytes of every file under a Store directory, summed from a stat of each.

    A stat opens no Store file, so this is no Store read under ADR 0023. A
    symlinked directory is not descended into.
    """
    return sum(path.stat().st_size for path in store_dir.rglob("*") if path.is_file())


__all__ = ["store_size_on_disk"]
