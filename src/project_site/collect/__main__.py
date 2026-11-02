"""`pixi run site-data`: run every collector, write `<data dir>/<source>.json`, print a line each (#389).

    pixi run site-data [--data-dir PATH] [--catalog-root PATH] [--registry PATH] [--strict]

A source that fails is written as `available: false` with its error, printed as
`UNAVAILABLE <source>: <error>`, and the others still run. The exit status is 0
anyway, because the pages show the notice; `--strict` exits 1 when any source
is unavailable, after every file is written, for a caller that must not go on.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from project_site.collect import architecture, board, catalog, common, records, releases, stores, work


def describe(result: dict[str, Any]) -> str:
    source = result["source"]
    if not result["available"]:
        return f"UNAVAILABLE {source}: {result['error']}"
    if source == "board":
        lines = sum(len(m["lines"]) for m in result["milestones"])
        return f"board: {len(result['epics'])} epics, {len(result['milestones'])} milestones, {lines} milestone lines"
    if source == "catalog":
        return f"catalog: {len(result['versions'])} versions, current {result['current'] or 'none'}"
    if source == "releases":
        tags = sum(len(r["tags"]) for r in result["repos"])
        return f"releases: {sum(len(r['releases']) for r in result['repos'])} releases, {tags} tags"
    if source == "stores":
        return f"stores: {len(result['stores'])} stores"
    if source == "architecture":
        return _describe_architecture(result)
    if source == "work":
        return _describe_work(result)
    return (
        f"records: {len(result['epics'])} epics, {len(result['invalid'])} invalid, "
        f"{len(result['yardstick']['results'])} yardstick results, {len(result['yardstick']['invalid'])} invalid"
    )


def _describe_architecture(result: dict[str, Any]) -> str:
    engine, summary, gaps = result["engine"], result["summary"], result["gaps"]
    counts = {repo: sum(1 for c in result["components"] if c["repo"] == repo) for repo in ("registry", "engine")}
    stale = " REFRESH FAILED" if (engine.get("refresh") or {}).get("ok") is False else ""
    model = f"engine model at {engine['ref']} {engine['commit'][:8]}{stale}" if engine["model"] else (
        f"NO engine model at {engine['ref']} {engine['commit'][:8]}")
    # Without the engine's model the figures that need its layers and claims are
    # unknown (`null`), and say so rather than reading 0 (#421 review).
    upward = "unknown" if summary["upward_edges"] is None else (
        f"{summary['upward_edges']} upward edges ({summary['upward_imports']} imports)")
    return (
        f"architecture: {counts['registry']} registry + {counts['engine']} engine components ({model}), "
        f"gaps registry {len(gaps['registry']['unclaimed'])}/{len(gaps['registry']['stale'])} "
        f"engine {len(gaps['engine']['unclaimed'])}/{len(gaps['engine']['stale'])} (unclaimed/stale), "
        f"{upward}, "
        f"{summary['registry_to_engine_imports']} registry->engine imports, "
        f"{summary['adrs_placed']} of {summary['adrs_total']} ADRs placed"
    )


def _describe_work(result: dict[str, Any]) -> str:
    summary = result["summary"]
    failed = [s["repo"] for s in result["branches"]["sources"] if (s.get("fetch") or {}).get("ok") is False]
    return (
        f"work: {summary['owners']} owners, {summary['meets']} meets ({summary['coordinated']} coordinated, "
        f"{summary['uncoordinated']} uncoordinated, {summary['unlinkable']} unlinkable), {summary['branches']} branches"
        + (f", ENGINE SIDE NOT MEASURED" if result.get("unmeasured", {}).get("engine") else "")
        + (f", BRANCH FETCH FAILED for {', '.join(failed)}" if failed else "")
    )


def run(
    data_dir: Path,
    *,
    gh: common.GhRunner = common.gh_runner,
    catalog_root: Path | None = None,
    registry: Path | None = None,
    epics_dir: Path | None = None,
    yardstick_dir: Path | None = None,
    repo_root: Path | None = None,
    engine_git: Path | None = None,
    engine_ref: str = architecture.DEFAULT_ENGINE_REF,
    refresh_engine: bool = False,
    out: Callable[[str], Any] = print,
) -> list[dict[str, Any]]:
    collectors: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("board", lambda: board.collect(gh)),
        ("catalog", lambda: catalog.collect(catalog_root)),
        ("releases", lambda: releases.collect(gh)),
        ("stores", lambda: stores.collect(registry)),
        ("records", lambda: records.collect(epics_dir, yardstick_dir)),
        ("architecture", lambda: architecture.collect(
            repo_root, engine_git=engine_git, engine_ref=engine_ref, refresh_engine=refresh_engine)),
        # After board and architecture: it reads the files those two just wrote to `data_dir`.
        ("work", lambda: work.collect(
            data_dir, registry_root=repo_root, engine_git=engine_git, refresh=refresh_engine)),
    ]
    results = []
    for source, collect in collectors:
        result = common.collect_source(source, collect)
        common.write_source(data_dir, result)
        out(describe(result))
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="project_site.collect", description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=None,
                        help=f"where the JSON files go (default: ${common.DATA_DIR_ENV_VAR}, else docs/_data/)")
    parser.add_argument("--catalog-root", type=Path, default=None,
                        help="the catalog's versions directory (default: <artifact root>/catalog/versions)")
    parser.add_argument("--registry", type=Path, default=None,
                        help="directory of Release Bundles (default: stores/ of this checkout)")
    parser.add_argument("--engine-git", type=Path, default=None,
                        help="the engine's git directory (default: the site's bare clone under the work root)")
    parser.add_argument("--engine-ref", default=architecture.DEFAULT_ENGINE_REF,
                        help="the engine ref the architecture is read at (default: main)")
    parser.add_argument("--refresh-engine", action="store_true",
                        help="fetch the engine ref (and the branches of both repositories) first, and record whether that worked")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any source is unavailable")
    args = parser.parse_args(argv)
    results = run(args.data_dir or common.data_dir(), catalog_root=args.catalog_root,
                  registry=args.registry, engine_git=args.engine_git, engine_ref=args.engine_ref,
                  refresh_engine=args.refresh_engine)
    return 1 if args.strict and not all(r["available"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
