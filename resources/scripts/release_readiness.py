#!/usr/bin/env python3
"""Render the Store Release readiness reconciliation view (#217).

Reads only committed Release Bundles and the historical-evidence ledger. With
``--info`` it additionally makes one supported, read-only
``opengwasdb info --format json`` call per published Store to refresh the
format/runtime channel -- it never opens a Store itself and never runs a
build or a Store validation.

  python3 resources/scripts/release_readiness.py --format markdown
  python3 resources/scripts/release_readiness.py --info --format yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores import paths, readiness


def _collect_info(
    bundles: list,
    artifact_root: Path,
) -> dict[str, dict]:
    """Best-effort fresh `opengwasdb info` per published Store, never fatal."""
    info_by_store: dict[str, dict] = {}
    for bundle in bundles:
        store_path = paths.store_path(bundle.store_id, root=artifact_root)
        if not store_path.exists():
            print(
                f"info: {bundle.store_id}: no published Store at {store_path}",
                file=sys.stderr,
            )
            continue
        try:
            info_by_store[bundle.store_id] = readiness.store_info(store_path)
        except ValueError as exc:
            print(f"info: {bundle.store_id}: {exc}", file=sys.stderr)
    return info_by_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-root", type=Path, default=REPO_ROOT / "stores")
    parser.add_argument("--ledger", type=Path, default=readiness.DEFAULT_LEDGER)
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument(
        "--info",
        action="store_true",
        help="refresh the format/runtime channel with fresh `opengwasdb info` calls",
    )
    parser.add_argument("--format", choices=("markdown", "yaml"), default="markdown")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    ledger = readiness.load_ledger(args.ledger)
    artifact_root = args.artifact_root or Path(
        ledger.get("artifact_root") or paths.artifact_root()
    )
    bundles = readiness.discover_bundles(args.registry_root)
    info_by_store = _collect_info(bundles, artifact_root) if args.info else {}

    releases = readiness.reconcile_registry(
        ledger=ledger,
        bundle_objects=bundles,
        info_by_store=info_by_store,
    )
    if args.format == "yaml":
        rendered = readiness.dump_readiness_yaml(releases, ledger)
    else:
        rendered = readiness.render_readiness_markdown(releases, ledger)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output} ({len(releases)} releases)", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
