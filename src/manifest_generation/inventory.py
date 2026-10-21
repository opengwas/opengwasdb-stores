#!/usr/bin/env python3
"""Freeze and preflight the Source Inventory a release config names (#151, #283, #287).

Two operator commands, one per direction of the same seam, each taking any
release config (``docs/spec/release-config.md``):

    pixi run inventory-freeze --config <release config>   # connectors -> frozen inventory
    pixi run preflight --config <release config>          # prove it before reading rows

``freeze`` runs the shared freeze (``manifest_generation.freeze``, #283) over
the Source Collection's two connector files and the release's Selection, and
writes the frozen Source Inventory, its frozen connectors and its provenance
into ``source_collection.inventory.directory``. A frozen inventory is never
overwritten: when one already exists under the snapshot id, ``freeze`` checks
that it was frozen for this Source Collection and this Selection and writes
nothing. A changed Selection or connector is a new snapshot id.

``preflight`` reads the frozen inventory, its provenance, the declared
Reference Resources, small per-Analysis metadata and filesystem metadata. It
does not open an association file, does not recompute a source checksum, and
does not compute ancestry or effect scale. It exits non-zero on anything that
would make the run proceed against a snapshot it cannot trust.

The candidate entry point runs both as its first two stages
(``manifest_generation.generate_candidate``).

Usage:
    pixi run inventory-freeze --config <release config>
    pixi run preflight --config <release config> [--cores 32]
    python3 -m manifest_generation.inventory preflight --config <release config>
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from manifest_generation import REPO_ROOT
from manifest_generation.freeze import (
    FreezeError,
    freeze,
    frozen_paths,
    load_collection_intent,
    load_provenance,
    write_frozen_inventory,
)
from manifest_generation.release_config import ReleaseConfig, load_release_config
from manifest_generation.source_inventory import (
    InventoryError,
    frozen_for_release_problems,
    preflight,
    read_inventory,
    render_preflight_summary,
    selection_partition_problems,
)


@dataclass(frozen=True)
class FreezeOutcome:
    """What the freeze stage did for one release config."""

    provenance_path: Path
    inventory_path: Path
    #: True when this run wrote the inventory; False when it already existed.
    frozen: bool
    inventory_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provenance_path": str(self.provenance_path),
            "inventory_path": str(self.inventory_path),
            "frozen_by_this_run": self.frozen,
            "inventory_tsv_sha256": self.inventory_sha256,
        }


def freeze_release(config: ReleaseConfig, *, frozen_at: str | None = None) -> FreezeOutcome:
    """Freeze ``config``'s Source Inventory, or prove the existing one is this release's.

    Writes nothing over an existing file. An inventory already under the
    snapshot id must record the shared freeze of this Source Collection for
    exactly this Selection; a partial set of frozen files is refused, as is a
    missing inventory when the config names no connectors to freeze it from.
    """
    paths = frozen_paths(config.inventory_directory, config.inventory_snapshot_id)
    if paths.provenance.is_file():
        provenance = load_provenance(paths.provenance)
        if not isinstance(provenance, dict):
            raise InventoryError(f"{paths.provenance} is not a YAML mapping")
        problems = frozen_for_release_problems(provenance, paths.provenance, config)
        if problems:
            raise InventoryError(
                f"the frozen Source Inventory {config.inventory_snapshot_id} is not this "
                "release's: " + "; ".join(problems)
            )
        return FreezeOutcome(
            provenance_path=paths.provenance,
            inventory_path=paths.inventory,
            frozen=False,
            inventory_sha256=str(provenance.get("inventory_tsv_sha256", "")),
        )
    leftover = [str(path) for path in paths.all if path.exists()]
    if leftover:
        raise FreezeError(
            f"{', '.join(leftover)} already exist(s) without the provenance that binds them; a "
            "frozen Source Inventory is never overwritten -- remove the partial freeze or "
            "freeze under a new snapshot id"
        )
    if config.connectors is None:
        raise InventoryError(
            f"frozen Source Inventory {paths.provenance} does not exist, and the release config "
            f"{config.path} names no source_collection.connectors to freeze it from"
        )
    frozen = freeze(
        snapshot_id=config.inventory_snapshot_id,
        source_collection_id=config.source_collection_id,
        metadata_table=config.connectors.source_metadata_table,
        acquisition_manifest=config.connectors.acquisition_manifest,
        selection=config.selection,
        frozen_at=frozen_at,
    )
    paths.inventory.parent.mkdir(parents=True, exist_ok=True)
    digest = write_frozen_inventory(frozen, paths.inventory, paths.provenance)
    return FreezeOutcome(
        provenance_path=paths.provenance,
        inventory_path=paths.inventory,
        frozen=True,
        inventory_sha256=digest,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze_parser = subparsers.add_parser(
        "freeze", help="freeze the release's Source Inventory from its connector files"
    )
    freeze_parser.add_argument("--config", required=True, help="the release config")
    freeze_parser.add_argument("--frozen-at", help="ISO-8601 freeze timestamp (default: now, UTC)")

    check = subparsers.add_parser("preflight", help="prove the frozen inventory before reading any association row")
    check.add_argument(
        "--config",
        required=True,
        action="append",
        help="a release config; repeat to preflight Selections of one Source Collection together",
    )
    check.add_argument("--cores", type=int, help="planned worker count (default: config runtime.cores)")
    check.add_argument("--work-root", help="override the run's work/log root (default: config output.work_root)")
    check.add_argument("--report", help="machine-readable JSON report path (default: <work-root>/preflight/)")
    return parser.parse_args(argv)


def command_freeze(args: argparse.Namespace, repo_root: Path) -> int:
    config = load_release_config(_config_path(repo_root, args.config), repo_root)
    outcome = freeze_release(config, frozen_at=args.frozen_at)
    verb = "Frozen" if outcome.frozen else "Already frozen for this release"
    print(f"{verb}: Source Inventory {config.inventory_snapshot_id}")
    print(f"  inventory   {outcome.inventory_path}")
    print(f"  provenance  {outcome.provenance_path}")
    print(f"  sha256      {outcome.inventory_sha256}")
    print(f"  selection   {config.selection.as_dict()}")
    rows = read_inventory(outcome.inventory_path)
    print(f"  rows        {len(rows)} ({sum(1 for row in rows if row.ready)} ready)")
    return 0


def command_preflight(args: argparse.Namespace, repo_root: Path) -> int:
    if args.report and len(args.config) > 1:
        raise InventoryError(
            "--report names one report path, but several --config values were given; run each "
            "config separately or omit --report"
        )
    ok = True
    releases: list[tuple[str, str, Sequence[str]]] = []
    provenances: list[Path] = []
    for value in args.config:
        config = load_release_config(_config_path(repo_root, value), repo_root)
        if args.work_root:
            config = dataclasses.replace(config, work_root=Path(args.work_root))
        inventory_path = config.inventory_path
        if not inventory_path.is_file():
            raise InventoryError(
                f"frozen source inventory not found: {inventory_path}; freeze it first with "
                "'pixi run inventory-freeze --config <release config>'"
            )
        rows = read_inventory(inventory_path)
        result = preflight(
            inventory_path=inventory_path,
            provenance_path=config.inventory_provenance_path,
            rows=rows,
            config=config,
            repo_root=repo_root,
            cores=args.cores,
        )
        releases.append((config.label, config.source_collection_id, [row.analysis_id for row in rows]))
        provenances.append(config.inventory_provenance_path)

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
            ok = False
            continue
        print(f"Report: {report_path}")
        ok = ok and result.ok

    if len(releases) > 1:
        # The multi-config preflight: Selections of one Source Collection must
        # partition it, so drift in a rule or a bound fails by name (#290). The
        # collection's checksummed intent is what proves completeness: an
        # Analysis no Selection covers, or one no Selection could ever take, is
        # named even though every Selection's own bound still matches
        # (#290 review).
        try:
            intent = load_collection_intent(provenances[0])
        except FreezeError as exc:
            raise InventoryError(str(exc)) from exc
        problems = selection_partition_problems(releases, collection_ids=intent.analysis_ids)
        if problems:
            print("FAILED: the Selections do not partition the Source Collection:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            ok = False
        else:
            print(
                f"Selections partition {releases[0][1]}: "
                f"{len(releases)} Selections, {sum(len(ids) for _label, _c, ids in releases)} rows"
            )
    return 0 if ok else 1


def _config_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


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
