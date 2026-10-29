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

from project_site.collect import board, catalog, common, records, releases, stores


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
    return (
        f"records: {len(result['epics'])} epics, {len(result['invalid'])} invalid, "
        f"{len(result['yardstick']['results'])} yardstick results, {len(result['yardstick']['invalid'])} invalid"
    )


def run(
    data_dir: Path,
    *,
    gh: common.GhRunner = common.gh_runner,
    catalog_root: Path | None = None,
    registry: Path | None = None,
    epics_dir: Path | None = None,
    yardstick_dir: Path | None = None,
    out: Callable[[str], Any] = print,
) -> list[dict[str, Any]]:
    collectors: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("board", lambda: board.collect(gh)),
        ("catalog", lambda: catalog.collect(catalog_root)),
        ("releases", lambda: releases.collect(gh)),
        ("stores", lambda: stores.collect(registry)),
        ("records", lambda: records.collect(epics_dir, yardstick_dir)),
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
    parser.add_argument("--strict", action="store_true", help="exit 1 when any source is unavailable")
    args = parser.parse_args(argv)
    results = run(args.data_dir or common.data_dir(), catalog_root=args.catalog_root, registry=args.registry)
    return 1 if args.strict and not all(r["available"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
