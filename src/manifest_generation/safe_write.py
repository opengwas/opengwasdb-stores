"""Containment-checked, exclusive-temp text writes (parent BAF pattern).

A metadata write target and its temp must resolve inside an isolated root, so a
symlinked parent cannot redirect the write into another tree. The temp is
created with ``open("x")`` (``O_EXCL``), which refuses a stale regular temp and
a hardlink/symlink alias introduced after validation instead of truncating it.
A final symlink is refused; an existing final is replaced only when the caller
explicitly allows it.

This mirrors the guarded writer the parent refresh tool uses for its metadata
temporaries; it is deliberately small and dependency-free so the candidate
emitters can share it rather than copy an unsafe variant.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO


class SafeWriteError(ValueError):
    """A write target is outside its root, aliased, or already present."""


def ensure_write_within(target: Path, root: Path, *, follow_final: bool = True) -> None:
    """Every write target/temp must resolve inside ``root``.

    ``follow_final=False`` is for a path we intend to create whose *parent* must
    be inside the root but whose own name need not exist yet.
    """
    root_resolved = Path(root).resolve()
    if follow_final:
        resolved = Path(target).resolve()
    else:
        target = Path(target)
        resolved = target.parent.resolve() / target.name
    if not (resolved == root_resolved or root_resolved in resolved.parents):
        raise SafeWriteError(
            f"write target {target} escapes the isolated root {root_resolved}"
        )


def open_exclusive_partial(path: Path, root: Path) -> tuple[Path, TextIO]:
    """Create a fresh, exclusive ``<name>.partial`` temp inside ``root``.

    Refuses a symlinked final or temp and a pre-existing temp (a stale regular
    file or an inode alias), so a rerun cannot silently consume another row's
    temp. The caller owns the returned handle.
    """
    path = Path(path)
    partial = path.with_name(path.name + ".partial")
    for target in (path, partial):
        ensure_write_within(target, root)
        if target.is_symlink():
            raise SafeWriteError(f"refusing symlinked write target: {target}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = partial.open("x", newline="", encoding="utf-8")
    except FileExistsError as exc:
        raise SafeWriteError(f"refusing existing temporary file: {partial}") from exc
    return partial, handle


def atomic_write_text(
    path: Path, text: str, root: Path, *, overwrite: bool = False
) -> None:
    """Write ``text`` to ``path`` atomically through an exclusive temp in ``root``.

    ``overwrite`` defaults to ``False`` so an emitted artifact is never silently
    replaced; pass ``True`` for a tool's own derived plan view.
    """
    path = Path(path)
    if path.is_symlink():
        raise SafeWriteError(f"refusing symlinked final path: {path}")
    partial, handle = open_exclusive_partial(path, root)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise SafeWriteError(f"refusing symlinked final path: {path}")
        if path.exists() and not overwrite:
            raise SafeWriteError(f"refusing to overwrite existing target: {path}")
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


__all__ = [
    "SafeWriteError",
    "atomic_write_text",
    "ensure_write_within",
    "open_exclusive_partial",
]
