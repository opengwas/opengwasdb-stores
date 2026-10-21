#!/usr/bin/env python3
"""Run the Release Bundle contract and Manifest Generation's rules over every registered bundle.

The contract (`ogstores.bundle.check`) is what both workflows share; Manifest
Generation's own rules (`manifest_generation.generation_rules`) are not
part of it, but every committed bundle came through a generator, so CI holds
it to both (#250).

    pixi run bundle-check
    python3 -m manifest_generation.check_bundles [--registry-root stores]

It lives here rather than in `ogstores` because it applies Manifest
Generation's rules, and Store Release Build imports nothing from Manifest
Generation (ADR 0035).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ogstores import bundle
from manifest_generation import REPO_ROOT, generation_rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-root", type=Path, default=REPO_ROOT / "stores")
    registry = parser.parse_args(argv).registry_root
    bundle_dirs = sorted(
        path
        for path in registry.iterdir()
        if path.is_dir() and (path / "release.yaml").is_file()
    )
    failures = 0
    tolerated_bundles = 0
    for root in bundle_dirs:
        loaded = bundle.load(root.name, registry_root=registry)
        result = generation_rules.check_with_contract(loaded, registry_root=registry)
        if not result:
            gaps = result.tolerated
            if gaps:
                tolerated_bundles += 1
                notes = "; ".join(
                    f"{gap.count} {gap.detail} tolerated under {gap.citation}"
                    for gap in gaps
                )
                print(f"PASS {root.name} ({notes})")
            else:
                print(f"PASS {root.name}")
            continue
        failures += 1
        print(f"FAIL {root.name}")
        for error in result:
            print(f"  - {error}")

    summary = f"{len(bundle_dirs) - failures}/{len(bundle_dirs)} Release Bundles passed"
    if tolerated_bundles:
        summary += f", {tolerated_bundles} with tolerated gaps"
    print(summary)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
