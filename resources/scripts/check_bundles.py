#!/usr/bin/env python3
"""Run the Release Bundle contract over every registered bundle."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores import bundle


def main() -> int:
    registry = REPO_ROOT / "stores"
    bundle_dirs = sorted(
        path
        for path in registry.iterdir()
        if path.is_dir() and (path / "release.yaml").is_file()
    )
    failures = 0
    for root in bundle_dirs:
        errors = bundle.check(
            bundle.load(root.name, registry_root=registry),
            registry_root=registry,
        )
        if not errors:
            print(f"PASS {root.name}")
            continue
        failures += 1
        print(f"FAIL {root.name}")
        for error in errors:
            print(f"  - {error}")

    print(f"{len(bundle_dirs) - failures}/{len(bundle_dirs)} Release Bundles passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
