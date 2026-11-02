"""Where live work meets: the `work` source (#419, epic #416).

It reads what the `board` and `architecture` collectors wrote to the same data
directory, so it is unavailable, naming which, when either is. From those it
places every piece of live work on the components that code claims:

- `building`: a branch's diff against the merge-base with `main`;
- `active`: a file cited by an open ticket of an Active epic;
- `scope`: a file cited by an epic's body;
- `waiting`: a file cited by an open ticket blocked by an open ticket;
- `planned` / `paused`: a file cited by an open ticket of a Todo / Paused epic;
- `stale`: a branch whose epic or ticket is closed.

A meet is a component on which two or more owners (epics, or a branch or ticket
off the board) have `building` or `active` work. It is coordinated only when,
for every pair of those owners, a blocking edge links two specific tickets of
the pair, in either direction. Sharing an epic or having an edge elsewhere
between the epics does not count. The output is documented, and versioned by
`schema_version`, in `docs/spec/project-site.md`.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from project_site.collect import architecture_model as model_rules, branches as branch_reader, citations, plans as plan_reader
from project_site.collect.common import CollectError

SCHEMA_VERSION = 1
LIVE_STATUSES = ("Active", "Todo", "Paused")
LIVE_STATES = ("building", "active")
STATUS_STATE = {"Active": "active", "Todo": "planned", "Paused": "paused"}
REPO_KIND = {"opengwasdb": "engine", "opengwasdb-stores": "registry"}
KIND_REPO = {kind: repo for repo, kind in REPO_KIND.items()}
ENGINE_FETCH = "+refs/heads/*:refs/heads/*"
REGISTRY_FETCH = "+refs/heads/*:refs/remotes/origin/*"


def compute(
    board: Mapping[str, Any],
    architecture: Mapping[str, Any],
    branch_sets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The work output from the two collectors' documents and the branches read; no I/O."""
    components = [c for c in architecture.get("components") or [] if isinstance(c, Mapping)]
    claims = {
        kind: model_rules.claims_of(c for c in components if c["repo"] == kind and c.get("paths"))
        for kind in ("engine", "registry")
    }
    by_id = {c["id"]: c for c in components}
    # Without the engine's model no engine file belongs to any component, so engine-side
    # work is unknown: it is said to be, never turned into "no work" or "no meets".
    engine = architecture.get("engine") or {}
    unmeasured: dict[str, str] = {}
    if not engine.get("model"):
        unmeasured["engine"] = str(engine.get("note") or f"no architecture model at {engine.get('ref')}")

    listed = architecture.get("files")
    listings = {k: v for k, v in listed.items() if isinstance(v, list)} if isinstance(listed, Mapping) else {}

    is_code = {"registry": model_rules.is_registry_code, "engine": model_rules.is_engine_code}

    def place(strings: Sequence[str], home: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, list[str]]]]:
        """`(component, cited string, path status)` for each placed citation, and the ambiguous ones.

        The status is `found` (a tracked file), `not found` (a code path no tracked file
        is, nor ends with: attributed by its claim all the same), `unchecked` (no file
        inventory, or not a code path, so existence is not known) or `n/a` (a dotted module).
        """
        placed: list[tuple[str, str, str]] = []
        unresolved: list[tuple[str, list[str]]] = []
        for text in strings:
            status = "n/a"
            if "/" in text:
                got = citations.resolve_path(text, home, listings)
                if got.candidates:
                    unresolved.append((text, sorted(path for _, path in got.candidates)))
                    continue
                if got.found:
                    status, targets = "found", [got.found]
                else:
                    targets = [(kind, citations.without_line(text)) for kind in (home, *[k for k in claims if k != home])]
            else:
                targets = [(kind, path) for kind in (home, *[k for k in claims if k != home]) for path in _modules(text, kind)]
            for kind, path in targets:
                if cid := model_rules.owner(claims[kind], path):
                    if "/" in text and status != "found":
                        status = "not found" if kind in listings and is_code[kind](path) else "unchecked"
                    placed.append((cid, text, status))
                    break
        return placed, unresolved

    epics = {e["key"]: e for e in board.get("epics") or [] if e.get("state") == "OPEN" and e.get("status") in LIVE_STATUSES}
    every_epic = {e["key"]: e for e in board.get("epics") or []}
    ticket_epic = {c["key"]: e["key"] for e in every_epic.values() for c in e.get("children") or []}
    ticket_state = {c["key"]: c["state"] for e in every_epic.values() for c in e.get("children") or []}
    owners: dict[str, dict[str, Any]] = {
        key: {"key": key, "kind": "epic", "title": _title(e), "url": e.get("url"), "status": e["status"],
              "workstream": e.get("workstream")}
        for key, e in epics.items()
    }
    waits: list[tuple[str, str]] = []
    for e in epics.values():
        for child in e.get("children") or []:
            waits += [(child["key"], b["key"]) for b in child.get("blocked_by") or [] if b.get("state") == "OPEN"]
    blocked = {item for item, _ in waits}

    signals: dict[tuple[str, str, str, str, str, str], set[str]] = defaultdict(set)
    not_found_rows: set[tuple[str, str, str, str]] = set()
    unresolved_rows: set[tuple[str, str, str, tuple[str, ...]]] = set()

    def signal(cid: str, owner: str, item: str, via: str, state: str, what: Sequence[str], path: str = "found") -> None:
        signals[(cid, owner, item, via, state, path)].update(what)

    def cited_signals(owner: str, item: str, via: str, state: str, placed: Sequence[tuple[str, str, str]]) -> None:
        for cid, text, path in placed:
            signal(cid, owner, item, via, state, [text], path)
            if path == "not found":
                not_found_rows.add((owner, item, text, cid))

    for key, e in epics.items():
        home = REPO_KIND[key.split("#")[0]]
        placed, ambiguous = place(e.get("cited") or [], home)
        unresolved_rows |= {(key, key, text, tuple(paths)) for text, paths in ambiguous}
        cited_signals(key, key, "scope", "scope", placed)
        for child in e.get("children") or []:
            if child["state"] != "OPEN":
                continue
            state = "waiting" if child["key"] in blocked else STATUS_STATE[e["status"]]
            placed, ambiguous = place(child.get("cited") or [], REPO_KIND[child["key"].split("#")[0]])
            unresolved_rows |= {(key, child["key"], text, tuple(paths)) for text, paths in ambiguous}
            cited_signals(key, child["key"], "cited", state, placed)

    # A branch can be read from two clones (pushed, and a host's own): it counts once, and the
    # copy further ahead wins, because it holds commits the other lacks; a tie keeps the first read.
    chosen: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}
    sources = []
    for found in branch_sets:
        sources.append({k: found.get(k) for k in ("repo", "git_dir", "main", "days", "fetch")}
                       | {"local": any(b.get("local") for b in found["branches"]), "count": len(found["branches"])})
        for b in found["branches"]:
            key = (found["repo"], b["name"])
            if key not in chosen or b["ahead"] > chosen[key][1]["ahead"]:
                chosen[key] = (found["repo"], b)
    listed: list[dict[str, Any]] = []
    for repo, b in chosen.values():
        owner, item, state = _branch_owner(b, repo, epics, every_epic, ticket_epic, ticket_state, owners)
        cids = sorted({cid for f in b["files"] if (cid := model_rules.owner(claims[REPO_KIND[repo]], f))})
        for cid in cids:
            signal(cid, owner, item, "branch", state, [f"{repo}:{b['name']}"])
        listed.append({**b, "owner": owner, "item": item, "components": cids})

    rows = [
        {"component": cid, "owner": owner, "item": item, "via": via, "state": state, "path": path, "what": sorted(what)}
        for (cid, owner, item, via, state, path), what in sorted(signals.items())
    ]
    live: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        if row["state"] in LIVE_STATES:
            live[row["component"]][row["owner"]].add(row["item"])
    meets = []
    for cid, by_owner in sorted(live.items()):
        if len(by_owner) < 2:
            continue
        names = sorted(by_owner)
        pairs = []
        for a, b in combinations(names, 2):
            pair = classify_pair(waits, by_owner[a], by_owner[b])
            pairs.append({"owners": [a, b], "coordinated": pair["coordination"] == "coordinated", **pair})
        kinds = {p["coordination"] for p in pairs}
        meets.append({"component": cid, "owners": names, "items": {o: sorted(by_owner[o]) for o in names},
                      "pairs": pairs, "coordinated": kinds == {"coordinated"},
                      "coordination": "uncoordinated" if "uncoordinated" in kinds
                      else "unlinkable" if "unlinkable" in kinds else "coordinated"})
    with_signals = {row["owner"] for row in rows}
    return {
        "schema_version": SCHEMA_VERSION,
        "inputs": {
            "board": {"collected_at": board.get("collected_at")},
            "architecture": {
                "collected_at": architecture.get("collected_at"),
                "engine": {k: (architecture.get("engine") or {}).get(k) for k in ("ref", "commit")},
                "registry": {k: (architecture.get("registry") or {}).get(k) for k in ("ref", "commit")},
            },
        },
        "owners": [owners[k] for k in sorted(owners) if k in with_signals],
        "components": [
            {"id": cid, "name": by_id[cid]["name"], "repo": by_id[cid]["repo"]}
            for cid in sorted({r["component"] for r in rows})
        ],
        "unmeasured": unmeasured,
        "signals": rows,
        "unresolved": [
            {"owner": o, "item": i, "cited": text, "candidates": list(paths)}
            for o, i, text, paths in sorted(unresolved_rows)
        ],
        "not_found": [
            {"owner": o, "item": i, "cited": text, "component": c} for o, i, text, c in sorted(not_found_rows)
        ],
        "meets": meets,
        "plans": plan_reader.listed(board, architecture),
        "tickets": {c["key"]: key for key, e in epics.items() for c in e.get("children") or [] if c["state"] == "OPEN"},
        "waits": [{"item": x, "on": y} for x, y in waits],
        "branches": {"sources": sources, "list": sorted(listed, key=lambda b: (b["repo"], b["name"]))},
        "summary": {
            "owners": len(with_signals),
            "meets": len(meets),
            "coordinated": sum(1 for m in meets if m["coordinated"]),
            "uncoordinated": sum(1 for m in meets if m["coordination"] == "uncoordinated"),
            "unlinkable": sum(1 for m in meets if m["coordination"] == "unlinkable"),
            "branches": len(listed),
            "unresolved": len(unresolved_rows),
            "not_found": len(not_found_rows),
        },
    }


def classify_pair(waits: Sequence[tuple[str, str]], one: Sequence[str], other: Sequence[str]) -> dict[str, Any]:
    """How two owners' items on one component are coordinated: the one classifier the collector and the CLI share.

    `coordination` is `coordinated` when a blocking edge `(waiting, on)` links an item of
    one with an item of the other, in either direction; else `unlinkable` when one side's
    work is an epic's integration branch (no ticket an edge could link); else `uncoordinated`.
    `edges` are the `{waiting, on}` links, and `epic_branches` the integration branches
    involved, empty when an edge coordinates the pair.
    """
    one_set, other_set = set(one), set(other)
    edges = [
        {"waiting": x, "on": y} for x, y in waits
        if (x in one_set and y in other_set) or (x in other_set and y in one_set)
    ]
    # An epic's integration branch belongs to the epic as a whole: no ticket for an edge to link.
    epic_branches = epic_level_branches([*one_set, *other_set])
    kind = "coordinated" if edges else "unlinkable" if epic_branches else "uncoordinated"
    return {"coordination": kind, "edges": edges, "epic_branches": [] if edges else epic_branches}


def epic_level_branches(items: Sequence[str]) -> list[str]:
    """`repo:name` of each item that is an epic's integration branch, sorted.

    The one classifier the collector and the CLI share. A branch item
    (`branch:<repo>:<name>`) is epic-level when its name maps to an epic and to no
    ticket (`orch/<epic>-slug`): it belongs to the epic as a whole, so no blocking
    edge can link it to a ticket. That is decided by the branch's own name, never by
    whether the board happens to list the epic.
    """
    found = []
    for item in items:
        if not item.startswith("branch:"):
            continue
        label = item[len("branch:"):]
        repo, _, name = label.partition(":")
        epic, ticket = branch_reader.named_for(name, repo)
        if epic and not ticket:
            found.append(label)
    return sorted(found)


def _title(epic: Mapping[str, Any]) -> str:
    title = str(epic.get("title") or epic["key"])
    return title[len("Epic:"):].strip() if title.lower().startswith("epic:") else title


def _modules(cited: str, kind: str) -> list[str]:
    """The files a dotted module name can mean in repository `kind`, longest first."""
    parts = cited.split(".")
    root = "src/" if kind == "registry" else ""
    paths = []
    while parts:
        base = root + "/".join(parts)
        paths += [f"{base}.py", f"{base}/__init__.py"]
        parts.pop()
    return paths


def _branch_owner(
    b: Mapping[str, Any], repo: str, epics: Mapping[str, Any], every_epic: Mapping[str, Any],
    ticket_epic: Mapping[str, str], ticket_state: Mapping[str, str], owners: dict[str, dict[str, Any]],
) -> tuple[str, str, str]:
    """`(owner, item, state)` of a branch, adding an owner that is not a live epic of the board."""
    epic, ticket = b.get("epic"), b.get("ticket")
    if epic:
        other = KIND_REPO["registry" if REPO_KIND[repo] == "engine" else "engine"]
        epic = next((k for k in (epic, f"{other}#{epic.split('#')[1]}") if k in every_epic), epic)
    if not epic and ticket:
        epic = ticket_epic.get(ticket)
    owner = epic or ticket
    item = ticket or f"branch:{repo}:{b['name']}"
    if owner is None:
        owner = f"branch:{repo}:{b['name']}"
        owners[owner] = {"key": owner, "kind": "branch", "title": b["name"], "url": None, "status": "Unlinked branch",
                         "workstream": None}
        return owner, item, "building"
    if owner not in owners:
        known = every_epic.get(owner)
        status = known.get("status") or "Closed" if known else "Off board"
        owners[owner] = {"key": owner, "kind": "epic" if known else "issue", "title": _title(known) if known else owner,
                         "url": (known or {}).get("url"), "status": status, "workstream": (known or {}).get("workstream")}
    stale = (owner in every_epic and owner not in epics) or ticket_state.get(ticket or "") == "CLOSED"
    return owner, item, "stale" if stale else "building"


# ---------------------------------------------------------------------------
# Reading the other collectors and git
# ---------------------------------------------------------------------------


def _read(data_dir: Path, source: str) -> tuple[dict[str, Any] | None, str]:
    try:
        document = json.loads((data_dir / f"{source}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"{source}.json was not written"
    except (OSError, ValueError) as error:
        return None, f"{source}.json cannot be read: {error}"
    if not isinstance(document, dict) or not document.get("available"):
        return None, f"{source} is unavailable: {(document or {}).get('error') if isinstance(document, dict) else 'not an object'}"
    return document, ""


def repo_of(git_dir: Path) -> str:
    """Which of the two repositories a git directory is, by its `origin`, else by its package."""
    done = subprocess.run(["git", "-C", str(git_dir), "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    url = done.stdout.strip().removesuffix(".git").rstrip("/")
    if done.returncode == 0 and url:
        name = url.rsplit("/", 1)[-1]
        if name in REPO_KIND:
            return name
    tree = subprocess.run(["git", "-C", str(git_dir), "ls-tree", "--name-only", "-z", "HEAD"], capture_output=True, check=False)
    top = model_rules.split_nul(tree.stdout.decode("utf-8", "replace"))
    return "opengwasdb" if "opengwasdb" in top else "opengwasdb-stores"


def _has_ref(git_dir: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(git_dir), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          capture_output=True, check=False).returncode == 0


def read_branches(
    *, registry_root: Path, engine_git: Path, refresh: bool = False,
    local: Sequence[Path] = (), days: int = branch_reader.WINDOW_DAYS, now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Pushed branches of both repositories, then those of each local git directory given.

    The engine's clone holds them as `refs/heads/*`; this repository's checkout as
    `refs/remotes/origin/*`. `refresh` fetches each first, recording a failure.
    """
    registry_main = next((r for r in ("origin/main", "main") if _has_ref(registry_root, r)), "HEAD")
    sets = [
        # Ahead of the engine's `main`, whatever ref the architecture was read at.
        branch_reader.collect(engine_git, "opengwasdb", main_ref="main", prefixes=("refs/heads/",), days=days, now=now,
                              fetch=ENGINE_FETCH if refresh else None),
        branch_reader.collect(registry_root, "opengwasdb-stores", main_ref=registry_main, prefixes=("refs/remotes/origin/",),
                              days=days, now=now, fetch=REGISTRY_FETCH if refresh else None),
    ]
    for git_dir in local:
        main = next((r for r in ("origin/main", "main") if _has_ref(git_dir, r)), "HEAD")
        sets.append(branch_reader.collect(git_dir, repo_of(git_dir), main_ref=main,
                                          prefixes=("refs/heads/", "refs/remotes/origin/"), days=days, now=now, local=True))
    return sets


def collect(
    data_dir: Path,
    *,
    registry_root: Path | None = None,
    engine_git: Path | None = None,
    refresh: bool = False,
    local: Sequence[Path] = (),
    days: int = branch_reader.WINDOW_DAYS,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """The `work` source: the board and architecture files of `data_dir`, and the branches read now."""
    data_dir = Path(data_dir)
    board, board_why = _read(data_dir, "board")
    architecture, architecture_why = _read(data_dir, "architecture")
    if board is None or architecture is None:
        problems = [why for doc, why in ((board, board_why), (architecture, architecture_why)) if doc is None]
        raise CollectError("; ".join(problems))
    if registry_root is None or engine_git is None:
        from ogstores import paths
        from project_site import paths as site_paths

        registry_root = registry_root or paths.REPO_ROOT
        engine_git = engine_git or site_paths.engine_clone()
    sets = read_branches(registry_root=registry_root, engine_git=engine_git, refresh=refresh,
                         local=local, days=days, now=now)
    return compute(board, architecture, sets)
