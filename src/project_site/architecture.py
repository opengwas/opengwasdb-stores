"""`python3 -m project_site.architecture`: ask the architecture map from a terminal (#419, epic #416).

    check      [--registry-root P] [--engine-git P] [--engine-ref R] [--data DIR]
    conflicts  (--branch B [--repo engine|registry] | --files PATH... [--ticket repo#n])
               [--local GIT_DIR...] [--data DIR] [--refresh] [--days N]

`check` reports the code files no component claims and the claims that match no
file, in both repositories, and compares the engine's layering baseline with the
upward imports measured, entry by entry (file and imported name), naming new and stale ones. It exits 1 on any of them.

`conflicts` says which components a piece of work touches, which other owners have
live work (`building` or `active`) on each, and whether a blocking edge links its
ticket to theirs. It exits 1 when any such meet is uncoordinated, naming the other
work: its ticket, epic, branch and the files cited. Exit 2 means it could not decide
(a branch the collection does not hold, a source that is unavailable): never read
that as "no conflict".

Both run from data collected on the spot with the site's own collectors, or from an
existing collection with `--data DIR`. `--local GIT_DIR` adds the local branches of a
clone (an orchestrate host's lanes live there), which the published page never shows.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

import yaml

from project_site import paths as site_paths
from project_site.collect import architecture, board, branches, common, work

BASELINE_PATH = "docs/architecture/layering-baseline.yaml"
LIVE = ("building", "active")
UNDECIDED = 2


class Unavailable(RuntimeError):
    """The data needed to answer is not there; the command exits 2 and says why."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def _architecture(args: argparse.Namespace) -> dict[str, Any]:
    if args.data:
        document, why = work._read(Path(args.data), "architecture")
        if document is None:
            raise Unavailable(why)
        return document
    result = common.collect_source("architecture", lambda: architecture.collect(
        args.registry_root, engine_git=args.engine_git, engine_ref=args.engine_ref, refresh_engine=getattr(args, "refresh", False)))
    if not result["available"]:
        raise Unavailable(f"architecture is unavailable: {result['error']}")
    return result


def _work(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(work, architecture)`: a collection's own, or collected now, plus any `--local` branches."""
    if args.data and not args.local:
        document, why = work._read(Path(args.data), "work")
        if document is None:
            raise Unavailable(why)
        return document, _architecture(args)
    # Scratch under the site's work root (/data), never the host's /tmp (CONTRIBUTING, Worktrees).
    scratch_root = site_paths.work_root()
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="architecture-cli-", dir=scratch_root) as scratch:
        directory = Path(args.data) if args.data else Path(scratch)
        if not args.data:
            for source, collect in (("board", lambda: board.collect(common.gh_runner)),
                                    ("architecture", lambda: architecture.collect(
                                        args.registry_root, engine_git=args.engine_git, engine_ref=args.engine_ref,
                                        refresh_engine=args.refresh))):
                common.write_source(directory, common.collect_source(source, collect))
        try:
            document = work.collect(
                directory, registry_root=args.registry_root, engine_git=args.engine_git,
                refresh=args.refresh, local=[Path(p) for p in args.local], days=args.days)
        except common.CollectError as error:
            raise Unavailable(f"work is unavailable: {error}") from error
        architecture_doc, why = work._read(directory, "architecture")
        if architecture_doc is None:
            raise Unavailable(why)
        return document, architecture_doc


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def check(args: argparse.Namespace, out: TextIO) -> int:
    document = _architecture(args)
    failed = False
    names = (("registry", "this repository"), ("engine", "the engine"))
    engine = document["engine"]
    for repo, label in names:
        if repo == "engine" and not engine.get("model"):
            print(f"{label} ({engine['ref']} {str(engine['commit'])[:8]}): no architecture model there, so nothing to check", file=out)
            continue
        gap = document["gaps"][repo]
        suffix = f" ({engine['ref']} {str(engine['commit'])[:8]})" if repo == "engine" else ""
        print(f"{label}{suffix}: {len(gap['unclaimed'])} unclaimed, {len(gap['stale'])} stale", file=out)
        for path in gap["unclaimed"]:
            print(f"  unclaimed: {path}", file=out)
        for claim in gap["stale"]:
            print(f"  stale: {claim['path']} (claimed by {claim['component']})", file=out)
        failed |= bool(gap["unclaimed"] or gap["stale"])
    if engine.get("model"):
        failed |= _baseline(args, document, out)
    return 1 if failed else 0


def _baseline(args: argparse.Namespace, document: Mapping[str, Any], out: TextIO) -> bool:
    """Print how the engine's baseline compares with the measured upward imports; True when it does not match."""
    engine = document["engine"]
    try:
        git_dir = args.engine_git or architecture._default_engine_clone()
        text = architecture.Tree(Path(git_dir), engine["commit"]).read([BASELINE_PATH]).get(BASELINE_PATH)
    except common.CollectError as error:
        print(f"layering baseline: not read ({error})", file=out)
        return False
    if text is None:
        print(f"no layering baseline at {engine['ref']} ({BASELINE_PATH} is not there)", file=out)
        return False
    listed = {(e["file"], e["name"]) for e in (yaml.safe_load(text) or {}).get("allowed") or []}
    found = {(u["file"], u["name"]): u for u in document.get("upward_imports") or []}
    print(f"layering baseline: {len(listed)} allowed, {len(found)} measured upward imports", file=out)
    names = {c["id"]: c["name"] for c in document["components"]}
    for key in sorted(set(found) - listed):
        u = found[key]
        print(f"  new: {key[0]} {key[1]} ({names.get(u['from'], u['from'])}, layer {u['from_layer']} -> "
              f"{names.get(u['to'], u['to'])}, layer {u['to_layer']}): not in the baseline", file=out)
    for key in sorted(listed - set(found)):
        print(f"  stale: {key[0]} {key[1]}: listed but no longer imported upward", file=out)
    return listed != set(found)


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------


def conflicts(args: argparse.Namespace, out: TextIO) -> int:
    document, architecture_doc = _work(args)
    components = {c["id"]: c for c in architecture_doc["components"]}
    own_owner, own_items, touched, label = _candidate(args, document, architecture_doc)
    print(label, file=out)
    waits = [(w["item"], w["on"]) for w in document["waits"]]
    owners = {o["key"]: o for o in document["owners"]}
    uncoordinated = False
    any_meet = False
    for cid in sorted(touched):
        name = components[cid]["name"] if cid in components else cid
        others: dict[str, list[Mapping[str, Any]]] = {}
        for signal in document["signals"]:
            if signal["component"] == cid and signal["state"] in LIVE and signal["owner"] != own_owner:
                others.setdefault(signal["owner"], []).append(signal)
        if not others:
            continue
        any_meet = True
        print(f"{name} ({cid}) meets {len(others)} other owner{'s' if len(others) != 1 else ''}:", file=out)
        for owner, signals in sorted(others.items()):
            title = (owners.get(owner) or {}).get("title", "")
            print(f"  {owner} {title}".rstrip(), file=out)
            for s in signals:
                print(f"    {s['item']} [{s['state']}, {s['via']}] {', '.join(s['what'])}", file=out)
            theirs = {s["item"] for s in signals}
            edges = [(x, y) for x, y in waits if (x in own_items and y in theirs) or (y in own_items and x in theirs)]
            if edges:
                print("    coordinated: " + "; ".join(f"{x} waits for {y}" for x, y in edges), file=out)
            else:
                uncoordinated = True
                mine = ", ".join(sorted(own_items)) or "this work"
                print(f"    UNCOORDINATED: no blocking edge links {mine} and {', '.join(sorted(theirs))}", file=out)
    if not any_meet:
        print("meets no other work", file=out)
    return 1 if uncoordinated else 0


def _candidate(
    args: argparse.Namespace, document: Mapping[str, Any], architecture_doc: Mapping[str, Any]
) -> tuple[str | None, set[str], set[str], str]:
    """`(owner, items, components touched, a heading)` of the work being asked about."""
    ticket = args.ticket
    if args.branch:
        found = [b for b in document["branches"]["list"]
                 if b["name"] == args.branch and (not args.repo or work.REPO_KIND.get(b["repo"]) == args.repo)]
        if not found:
            raise Unavailable(
                f"{args.branch}: no live branch of that name in the collection (ahead of main, within its window); "
                "add its git directory with --local, or give --files")
        b = found[0]
        items = {ticket} if ticket else {b["item"]}
        owner = document["tickets"].get(ticket) if ticket else b["owner"]
        return owner, items, set(b["components"]), (
            f"{b['repo']}:{b['name']}: {b['item']} of {b['owner']}, {len(b['files'])} files on "
            f"{len(b['components'])} components")
    claims = {
        kind: work.model_rules.claims_of(c for c in architecture_doc["components"] if c["repo"] == kind and c.get("paths"))
        for kind in ("registry", "engine")
    }
    kinds = [args.repo] if args.repo else ["registry", "engine"]
    touched = {cid for f in args.files for kind in kinds if (cid := work.model_rules.owner(claims[kind], f))}
    owner = document["tickets"].get(ticket) if ticket else None
    return owner, {ticket} if ticket else set(), touched, (
        f"{len(args.files)} files on {len(touched)} components" + (f" for {ticket}" if ticket else ""))


# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="project_site.architecture", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "conflicts"):
        p = sub.add_parser(name)
        p.add_argument("--data", type=Path, default=None, help="read this collection (docs/_data) instead of collecting now")
        p.add_argument("--registry-root", type=Path, default=None, help="this repository's git directory or checkout (default: this checkout)")
        p.add_argument("--engine-git", type=Path, default=None, help="the engine's git directory (default: the site's bare clone)")
        p.add_argument("--engine-ref", default=architecture.DEFAULT_ENGINE_REF)
        p.add_argument("--refresh", action="store_true", help="fetch the engine and both repositories' branches first")
    conflict = sub.choices["conflicts"]
    what = conflict.add_mutually_exclusive_group(required=True)
    what.add_argument("--branch")
    what.add_argument("--files", nargs="+", metavar="PATH")
    conflict.add_argument("--ticket", help="the ticket the work is for (repo#n), to look for a blocking edge")
    conflict.add_argument("--repo", choices=("engine", "registry"), help="which repository the branch or files belong to")
    conflict.add_argument("--local", nargs="+", default=[], metavar="GIT_DIR", help="add the local branches of these clones")
    conflict.add_argument("--days", type=int, default=branches.WINDOW_DAYS)
    args = parser.parse_args(argv)
    try:
        return (check if args.command == "check" else conflicts)(args, out)
    except Unavailable as error:
        print(f"cannot decide: {error}", file=out)
        return UNDECIDED


if __name__ == "__main__":
    sys.exit(main())
