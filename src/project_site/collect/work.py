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

from project_site.collect import architecture_model as model_rules, branches as branch_reader
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

    def place(strings: Sequence[str], home: str) -> dict[str, list[str]]:
        """Component id -> the cited strings that land on it (a path, or a dotted module)."""
        found: dict[str, list[str]] = defaultdict(list)
        for text in strings:
            for kind in (home, *[k for k in claims if k != home]):
                cid = next((c for p in _paths(text, kind) if (c := model_rules.owner(claims[kind], p))), None)
                if cid:
                    found[cid].append(text)
                    break
        return found

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

    signals: dict[tuple[str, str, str, str, str], set[str]] = defaultdict(set)

    def signal(cid: str, owner: str, item: str, via: str, state: str, what: Sequence[str]) -> None:
        signals[(cid, owner, item, via, state)].update(what)

    for key, e in epics.items():
        home = REPO_KIND[key.split("#")[0]]
        for cid, cited in place(e.get("cited") or [], home).items():
            signal(cid, key, key, "scope", "scope", cited)
        for child in e.get("children") or []:
            if child["state"] != "OPEN":
                continue
            state = "waiting" if child["key"] in blocked else STATUS_STATE[e["status"]]
            for cid, cited in place(child.get("cited") or [], REPO_KIND[child["key"].split("#")[0]]).items():
                signal(cid, key, child["key"], "cited", state, cited)

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
        {"component": cid, "owner": owner, "item": item, "via": via, "state": state, "what": sorted(what)}
        for (cid, owner, item, via, state), what in sorted(signals.items())
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
            edges = [
                {"waiting": x, "on": y} for x, y in waits
                if (x in by_owner[a] and y in by_owner[b]) or (x in by_owner[b] and y in by_owner[a])
            ]
            pairs.append({"owners": [a, b], "coordinated": bool(edges), "edges": edges})
        meets.append({"component": cid, "owners": names, "items": {o: sorted(by_owner[o]) for o in names},
                      "pairs": pairs, "coordinated": all(p["coordinated"] for p in pairs)})
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
        "signals": rows,
        "meets": meets,
        "waits": [{"item": x, "on": y} for x, y in waits],
        "branches": {"sources": sources, "list": sorted(listed, key=lambda b: (b["repo"], b["name"]))},
        "summary": {
            "owners": len(with_signals),
            "meets": len(meets),
            "coordinated": sum(1 for m in meets if m["coordinated"]),
            "uncoordinated": sum(1 for m in meets if not m["coordinated"]),
            "branches": len(listed),
        },
    }


def _title(epic: Mapping[str, Any]) -> str:
    title = str(epic.get("title") or epic["key"])
    return title[len("Epic:"):].strip() if title.lower().startswith("epic:") else title


def _paths(cited: str, kind: str) -> list[str]:
    """The files a cited string can mean in repository `kind`: itself, or a dotted module's file."""
    if "/" in cited:
        return [cited]
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
