"""Which tracked file a cited path means: the one resolver of tickets and ADRs (#416).

A ticket or ADR writes `opengwasdb/encoding/codec.py`, or `encoding/codec.py`
(relative to the package), or `layouts/ragged/zarr_csr.py:551` (with a line).
A cited path means a tracked file when it is that file, or when **exactly one**
tracked file of either repository ends with `/<path>`. When several do it means
none: the citation is *unresolved*, naming its candidates, never guessed.
"""

from __future__ import annotations

import re
from typing import Mapping, NamedTuple, Sequence

LINE_SUFFIX = re.compile(r":\d+(?:[-:]\d+)*$")


class Resolution(NamedTuple):
    """`found` is `(repository kind, file)`, or `None`; `candidates` is non-empty only when ambiguous."""

    found: tuple[str, str] | None
    candidates: tuple[tuple[str, str], ...]


def without_line(cited: str) -> str:
    """`cited` without a trailing `:line` (or `:line-line`)."""
    return LINE_SUFFIX.sub("", cited)


def resolve_path(cited: str, home: str, listings: Mapping[str, Sequence[str]]) -> Resolution:
    """The tracked file `cited` means, looking in repository `home` first.

    `listings` maps a repository kind to its tracked files. An exact file wins (the
    home repository's first); otherwise a single file ending `/<cited>` in either.
    """
    path = without_line(cited)
    order = [home, *[kind for kind in listings if kind != home]]
    for kind in order:
        if path in set(listings.get(kind) or ()):
            return Resolution((kind, path), ())
    hits = [(kind, f) for kind in order for f in listings.get(kind) or () if f.endswith("/" + path)]
    if len(hits) == 1:
        return Resolution(hits[0], ())
    return Resolution(None, tuple(hits))
