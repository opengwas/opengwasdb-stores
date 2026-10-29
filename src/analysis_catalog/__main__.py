"""`pixi run catalog`: rebuild the Analysis Catalog from the registry's Release Bundles.

    pixi run catalog [--registry stores] [--artifact-root PATH]

The Artifact Root resolves as everywhere else (`ogstores.paths.artifact_root`):
`--artifact-root`, then `OPENGWASDB_ARTIFACT_ROOT`, then `ogstores.yaml`. Run it
after a merge to `main`, from a dedicated clean checkout of `origin/main`
(`docs/spec/analysis-catalog.md`). A rebuild with unchanged inputs writes
nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from ogstores import paths

from analysis_catalog.build import CatalogError, build_catalog
from analysis_catalog.publish import catalog_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analysis_catalog", description=__doc__.split("\n")[0])
    parser.add_argument("--registry", type=Path, default=paths.REPO_ROOT / "stores",
                        help="directory of Release Bundles (default: stores/ of this checkout)")
    parser.add_argument("--artifact-root", type=Path, default=None,
                        help="Artifact Root to publish under (default: resolved as ogstores does)")
    args = parser.parse_args(argv)
    root = paths.artifact_root(args.artifact_root)
    try:
        result = build_catalog(args.registry, root)
    except CatalogError as exc:
        print(f"catalog: refused: {exc}", file=sys.stderr)
        return 1
    tally = Counter(d.decision for d in result.decisions)
    print(f"registered: {len(result.decisions)} ("
          + ", ".join(f"{name} {tally[name]}" for name in ("listed", "placeholder", "excluded", "refused")) + ")")
    for decision in result.decisions:
        print(f"  {decision.store_id}: {decision.decision}: {decision.reason}")
    print(f"version: {result.version}")
    if result.published:
        print(f"published: {result.path}")
    elif result.repointed:
        print(f"no new version; current moved to {result.version}")
    else:
        print("unchanged: no-op, nothing written")
    print(f"current: {catalog_dir(root) / 'current'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
