"""Who owns which file: the claims of an architecture model (#418, epic #416).

Both repositories keep a `docs/architecture/components.yaml` whose components
claim code paths. A path ending in `/` claims a directory and any other path
claims one file; the longest claimed prefix wins, so a file belongs to exactly
one component. These functions are the one implementation of that rule: this
repository's ownership test fails on what they report, and the site's collector
reports the same things for the engine without failing the build.

They take plain data (a model, a list of tracked paths) and read no repository.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

Claims = list[tuple[str, str]]

# What this repository counts as code, which every claim must keep covered.
REGISTRY_CODE_ROOTS = ("src", "workflow", "tools")
REGISTRY_CODE_SUFFIXES = (".py", ".smk", ".sh", ".R", ".r")
ENGINE_PACKAGE = "opengwasdb"


def split_nul(listing: str) -> list[str]:
    """The paths of a NUL-separated git listing (`ls-files -z`, `ls-tree -z`), each whole.

    Git quotes a path holding a tab or newline in its line-separated form, which
    would stop it matching any code root; the NUL form never does.
    """
    return listing.split("\0")[:-1] if listing.endswith("\0") else [p for p in listing.split("\0") if p]


def load_model(text: str) -> dict[str, Any]:
    """A model file's content as a mapping; a file that is not one is an error."""
    model = yaml.safe_load(text)
    if not isinstance(model, dict) or not isinstance(model.get("components"), list):
        raise ValueError("the architecture model must be a mapping with a `components` list")
    return model


def claims_of(components: Iterable[Mapping[str, Any]]) -> Claims:
    """Every `(claimed path, component id)` of `components`, in file order."""
    return [(path, str(component["id"])) for component in components for path in component.get("paths") or []]


def matches(claimed: str, path: str) -> bool:
    return path == claimed or (claimed.endswith("/") and path.startswith(claimed))


def owner(claims: Sequence[tuple[str, str]], path: str) -> str | None:
    """The component whose longest claimed prefix covers `path`, or `None`."""
    best: tuple[str, str] | None = None
    for claimed, component in claims:
        if matches(claimed, path) and (best is None or len(claimed) > len(best[0])):
            best = (claimed, component)
    return best[1] if best else None


def is_registry_code(path: str) -> bool:
    """A file of this repository that must be claimed: code under `src/`, `workflow/` or `tools/`."""
    if path.split("/", 1)[0] not in REGISTRY_CODE_ROOTS:
        return False
    return path.endswith(REGISTRY_CODE_SUFFIXES) or path.endswith("/Snakefile")


def is_engine_code(path: str) -> bool:
    """A file of the engine that must be claimed: a Python file of its package."""
    return path.startswith(f"{ENGINE_PACKAGE}/") and path.endswith(".py")


def check_claims(
    claims: Sequence[tuple[str, str]], tracked: Sequence[str], is_code: Callable[[str], bool]
) -> dict[str, list]:
    """The code files nobody claims and the claims that match no tracked file.

    `unclaimed` is a sorted list of paths; `stale` is a list of
    `{"path", "component"}` sorted by path.
    """
    unclaimed = sorted(path for path in tracked if is_code(path) and owner(claims, path) is None)
    stale = [
        {"path": claimed, "component": component}
        for claimed, component in sorted(claims)
        if not any(matches(claimed, path) for path in tracked)
    ]
    return {"unclaimed": unclaimed, "stale": stale}
