#!/usr/bin/env python3
"""Phase B inventory commands for the EBI GWAS Catalog eur-hybrid release.

Two operator commands, one per direction of the same seam (issue #151):

    pixi run inventory-freeze                 # manifests + candidates -> frozen inventory
    pixi run preflight                        # frozen inventory -> prove it before reading rows

``freeze`` is a pure transform over the acquisition manifests and the candidate
table: it merges the retry pass over the base pass, accounts every row against
the candidate pool, and writes the frozen inventory plus its provenance
sidecar. It never touches the mirror, so re-freezing the same inputs reproduces
the same bytes.

``preflight`` reads the frozen inventory, the declared Reference Resource
declarations, small per-Analysis metadata and filesystem metadata. It does not
open a GWAS-SSF association file, does not recompute a source checksum, and does
not compute ancestry or effect scale. It exits non-zero on anything that would
make the run proceed against a snapshot it cannot trust.

Both commands default to the full-release config, which names the snapshot it
selects from; ``--config`` and the per-input overrides exist for re-freezing a
later snapshot and for fixture-scale testing.

Usage:
    pixi run inventory-freeze
    pixi run preflight
    pixi run preflight --cores=32
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from resources.generators.lib.source_inventory import (  # noqa: E402
    InventoryError,
    PreflightConfigError,
    build_snapshot,
    load_release_configuration,
    preflight,
    read_candidate_selection,
    read_inventory,
    render_preflight_summary,
    write_snapshot,
)

FAMILY_DIR = "resources/generators/gwas-catalog-eur-hybrid"
DEFAULT_CONFIG = f"{FAMILY_DIR}/config-full.yaml"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("freeze", help="build the frozen Source Inventory from acquisition output")
    freeze.add_argument("--config", default=DEFAULT_CONFIG)
    freeze.add_argument("--base-manifest", help="acquisition pass status manifest (default: config)")
    freeze.add_argument("--retry-manifest", help="retry pass status manifest (default: config)")
    freeze.add_argument("--candidates", help="candidate pool table (default: config)")
    freeze.add_argument("--snapshot-id", help="freeze a different snapshot id (default: config); without --out-dir it is written beside the configured one")
    freeze.add_argument("--out-dir", help="write <snapshot-id>.tsv/.meta.yaml here (default: the configured inventory directory)")
    freeze.add_argument("--frozen-at", help="ISO-8601 freeze timestamp (default: now, UTC)")

    check = subparsers.add_parser("preflight", help="prove the frozen inventory before reading any association row")
    check.add_argument("--config", default=DEFAULT_CONFIG)
    check.add_argument("--inventory", help="override the inventory path the config declares")
    check.add_argument("--provenance", help="override the provenance sidecar path (default: config, or the inventory's sibling)")
    check.add_argument("--cores", type=int, help="planned worker count (default: config runtime.cores)")
    check.add_argument("--work-root", help="override the run's work/log root (default: config output.work_root)")
    check.add_argument("--report", help="machine-readable JSON report path (default: <work-root>/preflight/)")
    return parser.parse_args(argv)


def command_freeze(args: argparse.Namespace, repo_root: Path) -> int:
    config = load_release_configuration(_config_path(repo_root, args.config), repo_root)
    snapshot_id = args.snapshot_id or config.inventory_snapshot_id
    base_manifest = Path(args.base_manifest) if args.base_manifest else _freeze_input(config, "base_manifest")
    retry_manifest = Path(args.retry_manifest) if args.retry_manifest else _freeze_input(config, "retry_manifest")
    candidates_path = Path(args.candidates) if args.candidates else config.candidates_path

    candidates = read_candidate_selection(candidates_path, config.store_key)
    snapshot = build_snapshot(
        snapshot_id=snapshot_id,
        source_collection_id=config.source_collection_id,
        store_key=config.store_key,
        ancestry_group=config.ancestry_group,
        base_manifest=base_manifest,
        retry_manifest=retry_manifest,
        candidates=candidates,
        frozen_at=args.frozen_at,
    )
    if args.out_dir:
        out_dir = Path(args.out_dir)
    elif args.snapshot_id:
        # A new snapshot id names its own files, beside the configured one.
        out_dir = config.inventory_path.parent
    else:
        out_dir = None
    if out_dir is None:
        inventory_path = config.inventory_path
        provenance_path = config.inventory_provenance_path
    else:
        inventory_path = out_dir / f"{snapshot_id}.tsv"
        provenance_path = out_dir / f"{snapshot_id}.meta.yaml"

    digest = write_snapshot(snapshot, inventory_path, provenance_path)
    print(f"Frozen Source Inventory {snapshot_id}")
    print(f"  inventory   {inventory_path}")
    print(f"  provenance  {provenance_path}")
    print(f"  sha256      {digest}")
    print(f"  rows        {len(snapshot.rows)}")
    print(
        "  readiness   "
        + ", ".join(f"{status}={count}" for status, count in snapshot.readiness_counts.items())
    )
    print(
        f"  ready       {len(snapshot.ready_rows)} Analyses, "
        f"{snapshot.ready_bytes / 1e12:.3f} TB compressed "
        f"({', '.join(f'{d}={c}' for d, c in snapshot.ready_study_design_counts.items())})"
    )
    for entry in snapshot.inputs:
        print(
            f"  input {entry.role:14s} {entry.rows} rows, {entry.overrides} overriding "
            f"({', '.join(f'{s}={c}' for s, c in entry.readiness_counts.items())})"
        )
    for group in snapshot.duplicates:
        print(f"  duplicate content {' = '.join(group.analysis_ids)} ({group.data_bytes} bytes)")
    return 0


def command_preflight(args: argparse.Namespace, repo_root: Path) -> int:
    config = load_release_configuration(_config_path(repo_root, args.config), repo_root)
    if args.work_root:
        config = dataclasses.replace(config, work_root=Path(args.work_root))
    inventory_path = Path(args.inventory) if args.inventory else config.inventory_path
    if not inventory_path.is_file():
        raise InventoryError(
            f"frozen source inventory not found: {inventory_path}; freeze it first with "
            "'pixi run inventory-freeze'"
        )
    rows = read_inventory(inventory_path)
    provenance_path = _provenance_path(args, config, inventory_path)

    result = preflight(
        inventory_path=inventory_path,
        provenance_path=provenance_path,
        rows=rows,
        config=config,
        repo_root=repo_root,
        cores=args.cores,
    )

    report_path = Path(args.report) if args.report else (
        config.work_root / "preflight" / f"{inventory_path.stem}.json"
    )
    report_error: OSError | None = None
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result.report, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        report_error = exc

    print(render_preflight_summary(result.report))
    if report_error is not None:
        print(f"ERROR: could not write report to {report_path}: {report_error}", file=sys.stderr)
        return 1
    print(f"Report: {report_path}")
    return 0 if result.ok else 1


def _config_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _provenance_path(args: argparse.Namespace, config, inventory_path: Path) -> Path:
    if args.provenance:
        return Path(args.provenance)
    if args.inventory:
        # An explicitly named inventory carries its sidecar beside it, so a copy
        # can be preflighted without retargeting the release config.
        return inventory_path.with_name(f"{inventory_path.stem}.meta.yaml")
    return config.inventory_provenance_path


def _freeze_input(config, role: str) -> Path:
    try:
        return Path(config.freeze_inputs[role])
    except KeyError:
        raise PreflightConfigError(
            f"{config.path}: source.inventory.freeze_inputs is missing required role {role!r}"
        ) from None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    try:
        if args.command == "freeze":
            return command_freeze(args, repo_root)
        return command_preflight(args, repo_root)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
