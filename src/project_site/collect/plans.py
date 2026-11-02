"""A ticket's planned change to the architecture: read it, and judge it once the ticket closes (#422, epic #416).

A ticket body may hold one fenced block whose info string is `architecture`,
holding YAML; the format is documented in `docs/spec/project-site.md`
(`### Planned changes`). `parse` reads it into the plan the board file carries,
`check_components` re-judges its component ids against the model once that is
known, and `evaluate` (from the `work` collector) checks a closed ticket's items
against what the architecture collector read.

A block that cannot be used is never dropped: it becomes `{"invalid": reason}`
and the page shows it. A ticket without a block has no plan at all.
"""

from __future__ import annotations

import re
from typing import Any, Collection, Mapping

import yaml

KEYS = ("adds", "moves", "drops", "absorbs")
ADD_KEYS = {"paths", "component", "name", "container", "layer"}
MOVE_KEYS = {"paths", "to"}
DROP = re.compile(r"\s*([\w.-]+)\s*->\s*([\w.-]+)\s*")
_FENCE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*(?P<info>[^`]*?)\s*$")


class Invalid(ValueError):
    """The block cannot be used; the message is the reason the page shows."""


def blocks(body: str | None) -> list[str]:
    """The text of every `architecture` fenced block of `body`; anything outside a fence is ignored."""
    found: list[str] = []
    current: list[str] | None = None
    opening = ""
    for line in (body or "").splitlines():
        fence = _FENCE.match(line)
        if opening:
            if fence and not fence.group("info") and fence.group("fence")[0] == opening[0] and len(fence.group("fence")) >= len(opening):
                if current is not None:
                    found.append("\n".join(current))
                opening, current = "", None
            elif current is not None:
                current.append(line)
            continue
        if fence:
            opening = fence.group("fence")
            current = [] if fence.group("info") == "architecture" else None
    if opening and current is not None:
        raise Invalid("the `architecture` block is never closed")
    return found


def parse(body: str | None, known: Collection[str] | None = None) -> dict[str, Any] | None:
    """The plan in `body`: `None` without a block, `{"invalid": reason}` when unusable, else the plan.

    `known` is the ids of the components that exist; without it a component's
    existence is not judged (`check_components` does that later).
    """
    try:
        found = blocks(body)
        if not found:
            return None
        if len(found) > 1:
            raise Invalid("more than one `architecture` block")
        plan = _read(found[0])
        return plan if known is None else _checked(plan, known)
    except Invalid as error:
        return {"invalid": str(error)}


def check_components(plan: Mapping[str, Any], known: Collection[str]) -> dict[str, Any]:
    """`plan`, or `{"invalid": reason}` when it names a component that is neither in `known` nor declared in `adds`."""
    if "invalid" in plan:
        return dict(plan)
    try:
        return _checked(dict(plan), known)
    except Invalid as error:
        return {"invalid": str(error)}


def _read(text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise Invalid(f"the block is not valid YAML: {str(error).splitlines()[0] if str(error) else error}") from error
    if document is None:
        raise Invalid("the block is empty")
    if not isinstance(document, dict):
        raise Invalid("the block must be a mapping of `adds`, `moves`, `drops` and `absorbs`")
    for key in document:
        if key not in KEYS:
            raise Invalid(f"unknown key `{key}`")
    for key in KEYS:
        if document.get(key) is not None and not isinstance(document[key], list):
            raise Invalid(f"`{key}` must be a list")
    return {
        "adds": [_add(item) for item in document.get("adds") or []],
        "moves": [_move(item) for item in document.get("moves") or []],
        "drops": [_drop(item) for item in document.get("drops") or []],
        "absorbs": [_text(item) for item in document.get("absorbs") or []],
    }


def _paths(item: Mapping[str, Any], what: str) -> list[str]:
    paths = item.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise Invalid(f"{what} needs a non-empty `paths` list")
    return list(paths)


def _add(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise Invalid("an add must be a mapping with `paths` and `component`")
    unknown = sorted(set(item) - ADD_KEYS)
    if unknown:
        raise Invalid(f"unknown key `{unknown[0]}` in an add")
    paths = _paths(item, "an add")
    if not isinstance(item.get("component"), str) or not item["component"]:
        raise Invalid("an add needs a `component`")
    return {"paths": paths, **{k: item[k] for k in ("component", "name", "container", "layer") if k in item}}


def _move(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise Invalid("a move must be a mapping with `paths` and `to`")
    unknown = sorted(set(item) - MOVE_KEYS)
    if unknown:
        raise Invalid(f"unknown key `{unknown[0]}` in a move")
    paths = _paths(item, "a move")
    if not isinstance(item.get("to"), str) or not item["to"]:
        raise Invalid("a move needs a `to` component")
    return {"paths": paths, "to": item["to"]}


def _drop(item: Any) -> dict[str, str]:
    match = DROP.fullmatch(item) if isinstance(item, str) else None
    if not match:
        raise Invalid(f"drop `{item}` is not `from -> to`")
    return {"from": match.group(1), "to": match.group(2)}


def _text(item: Any) -> str:
    if not isinstance(item, str):
        raise Invalid("an `absorbs` entry must be text")
    return item


def _checked(plan: dict[str, Any], known: Collection[str]) -> dict[str, Any]:
    declared = {a["component"] for a in plan["adds"]}
    for add in plan["adds"]:
        if add["component"] not in known and not add.get("name"):
            raise Invalid(f"component `{add['component']}` is new, so its add needs a `name`")
    mentioned = [m["to"] for m in plan["moves"]] + [d[k] for d in plan["drops"] for k in ("from", "to")]
    for cid in mentioned:
        if cid not in known and cid not in declared:
            raise Invalid(f"component `{cid}` is neither an existing component nor declared in `adds`")
    return plan
