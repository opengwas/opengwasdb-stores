"""A Store's size on disk: the one definition (#502).

Engine-free, so `register` and `ogstores.backfill_observed` record it without
importing the engine, and `ogstores.audit._store_bytes` will call it once
opengwas/opengwasdb-stores#498 has landed its changes to `audit.py`.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path


def _raise(error: OSError) -> None:
    """`os.walk`'s `onerror`: an unreadable directory is an error, never skipped."""
    raise error


def walk_no_follow(root: Path | str) -> Iterator[tuple[str, list[str], list[str]]]:
    """`os.walk` of `root` that follows no symbolic link to a directory, and raises on an unreadable
    directory rather than skipping it: the one such walk (`store_size_on_disk`, the LD record writer's links)."""
    return os.walk(root, onerror=_raise)  # never descends a linked directory


def store_size_on_disk(store_dir: Path) -> int:
    """The bytes of every regular file under a Store directory, summed from an lstat of each.

    A stat opens no Store file, so this is no Store read under ADR 0023. No
    symbolic link is followed, to a file or to a directory, so an overlay's
    size is its own files alone (#538). An unreadable directory raises rather
    than being skipped.
    """
    total = 0
    for root, _, files in walk_no_follow(store_dir):
        for name in files:
            status = os.lstat(os.path.join(root, name))
            if stat.S_ISREG(status.st_mode):
                total += status.st_size
    return total


__all__ = ["store_size_on_disk", "walk_no_follow"]
