"""The key gate two config readers share: a mapping with exactly the keys it reads (#340 review).

`manifest_generation.release_config` and the completion recipe it delegates to
refuse the same ways -- a value that is not a mapping, an unknown key, a
missing required key, a value that is not text -- and one implementation keeps
the wording, and the coercion, the same. Each caller names its own error class,
so a caller catches what its own schema raises.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["block", "text"]


def block(
    value: Any,
    where: str,
    required: Sequence[str] = (),
    optional: Sequence[str] = (),
    *,
    error: type[ValueError] = ValueError,
) -> Mapping:
    """``value`` as a mapping with every required key and no unknown one."""
    if not isinstance(value, Mapping):
        raise error(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in value if key not in required and key not in optional)
    if unknown:
        raise error(
            f"{where} has unknown key(s) {', '.join(unknown)}; it reads only "
            + ", ".join(list(required) + list(optional))
        )
    missing = [key for key in required if value.get(key) is None or value.get(key) == ""]
    if missing:
        raise error(f"{where} is missing required key(s) {', '.join(missing)}")
    return value


def text(value: Any, where: str, *, error: type[ValueError] = ValueError) -> str:
    """``value`` as non-blank text. A number is read as its text; a bool is not."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise error(f"{where} must be text, not {value!r}")
    parsed = str(value)
    if not parsed.strip():
        raise error(f"{where} must not be blank")
    return parsed
