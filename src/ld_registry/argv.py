"""A recorded engine command's options, read as the engine's command line reads them (#542).

`--flag value` and `--flag=value` are the same option. The record writer reads a
rebuild's inputs with it, and the rebuild reads and moves its paths with it, so
the two never disagree on which form a command may use.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence


def options(argv: Sequence[str]) -> list[tuple[str, str]]:
    """Each option in `argv` that has a value, and that value, in order: `--flag value` and `--flag=value`."""
    found: list[tuple[str, str]] = []
    for number, argument in enumerate(argv):
        if not argument.startswith("--"):
            continue
        if "=" in argument:
            flag, value = argument.split("=", 1)
            found.append((flag, value))
        elif number + 1 < len(argv) and not argv[number + 1].startswith("--"):
            found.append((argument, argv[number + 1]))
    return found


def values(argv: Sequence[str], flag: str) -> list[str]:
    """Every value `argv` gives `flag`, in order."""
    return [value for option, value in options(argv) if option == flag]


def rewrite_value(argument: str, rewrite: Callable[[str], str]) -> str:
    """`argument` with its value rewritten: the value of `--flag=value`, or the argument itself."""
    if argument.startswith("--") and "=" in argument:
        flag, value = argument.split("=", 1)
        return f"{flag}={rewrite(value)}"
    return rewrite(argument)
