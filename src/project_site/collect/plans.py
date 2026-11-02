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


# ---------------------------------------------------------------------------
# Judging a plan against what the architecture collector read
# ---------------------------------------------------------------------------


def listed(board: Mapping[str, Any], architecture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every ticket's plan for the `work` source: the plan, the components it involves, and, once closed, its checks.

    A closed ticket's items are `held` true or false, or `None` (unknown) when
    the engine's model is missing and the item needs it: absence of the model is
    not an unmet item. An open ticket's plan is carried and nothing is checked,
    and so is an invalid one. `absorbs` is carried and never checked.
    """
    from project_site.collect import architecture_model as rules

    components = [c for c in architecture.get("components") or [] if isinstance(c, Mapping)]
    by_id = {c["id"]: c for c in components}
    engine = architecture.get("engine") or {}
    model = bool(engine.get("model"))
    claims = {kind: rules.claims_of(c for c in components if c["repo"] == kind and c.get("paths")) for kind in ("registry", "engine")}
    files = architecture.get("files")
    registry_ref = architecture.get("registry") or {}
    missing = f"the engine's model is missing at {engine.get('ref')} ({str(engine.get('commit'))[:12]})"
    edges = {(e["from"], e["to"]): e["imports"] for e in architecture.get("edges") or [] if isinstance(e, Mapping)}

    def exists(path: str) -> tuple[bool | None, str]:
        """Whether `path` is a tracked file: only code files are listed, and only where the tree was read."""
        kind = "registry" if rules.is_registry_code(path) else "engine" if rules.is_engine_code(path) else None
        if not isinstance(files, Mapping):
            return None, "this architecture collection lists no tracked files"
        if kind is None:
            return None, "only code files are listed, so a non-code path's existence is not known"
        listing = files.get(kind)
        if listing is None:
            return None, f"the {kind} tree was not read"
        if path in listing:
            return True, ""
        ref = registry_ref if kind == "registry" else engine
        return False, f"no such file at {ref.get('ref')} ({str(ref.get('commit'))[:12]})"

    def claimed(path: str, target: str, kind: str) -> dict[str, Any]:
        present, why = exists(path)
        found = next((o for k in ("registry", "engine") if (o := rules.owner(claims[k], path))), None)
        if present is False:
            held = False
        elif found and found != target:
            held, why = False, f"claimed by `{found}`, not `{target}`"
        elif present is None:
            held = None
        elif found == target:
            held, why = True, f"claimed by `{target}`"
        elif model or path.split("/", 1)[0] in rules.REGISTRY_CODE_ROOTS:
            held, why = False, "no component claims it"
        else:
            held, why = None, missing
        return {"kind": kind, "path": path, "component": target, "held": held, "why": why}

    def dropped(a: str, b: str) -> dict[str, Any]:
        if not model and not all(by_id.get(x, {}).get("repo") == "registry" for x in (a, b)):
            return {"kind": "drops", "from": a, "to": b, "imports": None, "held": None, "why": missing}
        n = edges.get((a, b), 0)
        why = f"no import from `{a}` into `{b}`" if not n else f"{n} import{'s' if n != 1 else ''} from `{a}` into `{b}` remain{'s' if n == 1 else ''}"
        return {"kind": "drops", "from": a, "to": b, "imports": n, "held": not n, "why": why}

    found: list[dict[str, Any]] = []
    for epic in board.get("epics") or []:
        for ticket in epic.get("children") or []:
            plan = ticket.get("plan")
            if plan is None:
                continue
            if model:
                plan = check_components(plan, set(by_id))
            closed = ticket.get("state") == "CLOSED"
            checks: list[dict[str, Any]] = []
            involved: list[dict[str, Any]] = []
            if "invalid" not in plan:
                declared = {a["component"]: a for a in plan["adds"]}
                ids = sorted({*declared, *(m["to"] for m in plan["moves"]), *(x for d in plan["drops"] for x in (d["from"], d["to"]))})
                involved = [
                    {"id": i, "name": by_id[i]["name"], "repo": by_id[i]["repo"], "new": False} if i in by_id
                    else {"id": i, "name": declared[i].get("name") or i if i in declared else i, "repo": None, "new": i in declared}
                    for i in ids
                ]
                if closed:
                    checks = (
                        [claimed(p, a["component"], "adds") for a in plan["adds"] for p in a["paths"]]
                        + [claimed(p, m["to"], "moves") for m in plan["moves"] for p in m["paths"]]
                        + [dropped(d["from"], d["to"]) for d in plan["drops"]]
                    )
            found.append({
                "ticket": ticket["key"], "epic": epic["key"], "title": ticket.get("title"), "url": ticket.get("url"),
                "state": "closed" if closed else "open", "plan": plan, "components": involved, "checks": checks,
            })
    return sorted(found, key=lambda p: (p["epic"], p["ticket"]))
