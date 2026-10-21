#!/usr/bin/env python3
"""Render the Store Release readiness reconciliation view (#217).

Reads only committed Release Bundles and the historical-evidence ledger. Two
read-only flags reach the published Store through supported core interfaces:
``--info`` makes one ``opengwasdb info --format json`` call per Store for
manifest metadata, and ``--built`` reads each Store's Analysis IDs via
``read_analyses(open_store(path).analyses_path)`` for built-ID reconciliation.
Neither opens a Store directly, runs a build, or runs a Store validation.

  python3 -m ogstores.release_readiness --format markdown
  python3 -m ogstores.release_readiness --info --built --format yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def _collect_built(
    bundles: list,
    artifact_root: Path,
) -> dict[str, tuple[tuple[str, ...], str]]:
    """Best-effort supported core read of each Store's Analysis IDs, never fatal."""
    built_by_store: dict[str, tuple[tuple[str, ...], str]] = {}
    for bundle in bundles:
        store_path = paths.store_path(bundle.store_id, root=artifact_root)
        if not store_path.exists():
            print(
                f"built: {bundle.store_id}: no published Store at {store_path}",
                file=sys.stderr,
            )
            continue
        try:
            built_by_store[bundle.store_id] = readiness.store_built_ids(store_path)
        except Exception as exc:  # noqa: BLE001 - report, never crash the view
            print(f"built: {bundle.store_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return built_by_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-root", type=Path, default=paths.REPO_ROOT / "stores")
    parser.add_argument("--ledger", type=Path, default=readiness.DEFAULT_LEDGER)
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument(
        "--info",
        action="store_true",
        help="refresh manifest metadata with fresh `opengwasdb info` calls",
    )
    parser.add_argument(
        "--built",
        action="store_true",
        help=(
            "read each published Store's Analysis IDs through the supported core "
            "API (open_store/analyses_path/read_analyses) for built-ID reconciliation"
        ),
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
    built_by_store = _collect_built(bundles, artifact_root) if args.built else {}

    releases = readiness.reconcile_registry(
        ledger=ledger,
        bundle_objects=bundles,
        info_by_store=info_by_store,
        built_by_store=built_by_store,
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
