"""A Store's size on disk: the one definition (#502).

Engine-free, so `register` and `ogstores.backfill_observed` record it without
importing the engine, and `ogstores.audit._store_bytes` will call it once
opengwas/opengwasdb-stores#498 has landed its changes to `audit.py`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def _raise(error: OSError) -> None:
    """`os.walk`'s `onerror`: an unreadable directory is an error, never skipped."""
    raise error


def store_size_on_disk(store_dir: Path) -> int:
    """The bytes of every regular file under a Store directory, summed from an lstat of each.

    A stat opens no Store file, so this is no Store read under ADR 0023. No
    symbolic link is followed, to a file or to a directory, so an overlay's
    size is its own files alone (#538). An unreadable directory raises rather
    than being skipped.
    """
    total = 0
    for root, _, files in os.walk(store_dir, onerror=_raise):  # never descends a linked directory
        for name in files:
            status = os.lstat(os.path.join(root, name))
            if stat.S_ISREG(status.st_mode):
                total += status.st_size
    return total


__all__ = ["store_size_on_disk"]
