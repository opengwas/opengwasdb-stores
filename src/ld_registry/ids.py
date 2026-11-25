"""LD Store Registry ids: `OGL-` and five digits, flat and opaque (ADR 0022, ADR 0042).

The rule mirrors `ogstores.paths`'s Store Release id. An id is assigned once and
never reused; it is the record's directory name, `stores/ld/<ld_id>/`.
"""

from __future__ import annotations

import re

LD_ID_PATTERN: re.Pattern[str] = re.compile(r"\AOGL-\d{5}\Z")


def is_valid_ld_id(ld_id: object) -> bool:
    """Return True if `ld_id` is `OGL-` and five digits."""
    return isinstance(ld_id, str) and bool(LD_ID_PATTERN.fullmatch(ld_id))


def require_valid_ld_id(ld_id: object) -> str:
    """Return `ld_id` if valid; raise ValueError otherwise."""
    if not is_valid_ld_id(ld_id):
        raise ValueError(f"Invalid ld_id {ld_id!r}; must match pattern 'OGL-\\d{{5}}' (ADR 0042)")
    return ld_id  # type: ignore[return-value]

