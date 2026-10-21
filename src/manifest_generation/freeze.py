#!/usr/bin/env python3
"""Freeze a Source Inventory from a Selection of intent and what was acquired (#283, ADR 0036).

One freeze for every Source Collection. It reads exactly two connector files
and a Selection:

- the **Source Metadata Table** (``source_metadata_table``): one row per
  intended Analysis, in canonical columns;
- the **Acquisition Manifest** (``acquisition_manifest``): per Analysis, the
  file acquisition produced, or the recorded reason it could not;
- the **Selection** (``selection``): which intended Analyses this release takes.

It writes, side by side:

- ``<snapshot-id>.tsv``, the frozen Source Inventory in the
  ``source_inventory`` column format;
- ``<snapshot-id>.source-metadata.tsv`` and
  ``<snapshot-id>.acquisition-manifest.tsv``, the selected rows of each
  connector file, verbatim -- the connectors frozen with the inventory, so
  later generation reads them and never the originals
  (:func:`load_frozen_connectors`);
- ``<snapshot-id>.meta.yaml``, whose provenance records the Selection, each
  frozen connector's sha256, and each original's path and sha256 (with the
  sha256 of the passes an acquisition writer recorded beside the manifest).

The reconciliation fails loudly (:class:`ReconciliationError`) on:

- a selected Analysis with no Acquisition Manifest entry -- no file and no
  recorded reason there is none;
- an Acquisition Manifest entry no Analysis in the table claims;

and a connector file refuses a duplicate Analysis or file when it loads. Every
refusal is a ``FreezeError``.

An entry with a non-ready status is an *explained* absence: acquisition tried
and recorded why there is no file. It stays in the inventory as a non-ready
row, exactly as the readiness vocabulary always kept it, and never becomes a
release member (``source_inventory.READY_STATUSES``).

Freeze never lists a directory and never reads the mirror or a live source:
intent is the table, availability is the manifest. Two freezes of the same
inputs write the same bytes.

This is the only freeze. A release config drives it through
``pixi run inventory-freeze --config <release config>`` or the candidate entry
point's ``freeze`` stage (``manifest_generation.inventory.freeze_release``,
#287); the command line below takes the connector files directly.
``docs/spec/source-connectors.md`` specifies the inputs and the provenance.

Usage:
    python3 -m manifest_generation.freeze \\
        --source-metadata-table <table.tsv> --acquisition-manifest <manifest.tsv> \\
        --selection <release config or YAML with a selection block> \\
        --snapshot-id <id> --source-collection-id <id> --out-dir <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    AcquisitionManifest,
    Lineage,
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import FreezeError, listed, render_connector
from manifest_generation.selection import Selection, load_selection, select
from manifest_generation.source_inventory import (
    InventoryError,
    SourceInventoryRow,
    atomic_write,
    counts,
    inventory_counts,
    sha256_file,
    write_inventory,
)
from manifest_generation.source_metadata_table import (
    SOURCE_METADATA_TABLE,
    SourceMetadataTable,
    load_source_metadata_table,
)

__all__ = [
    "FreezeError",
    "FrozenInventory",
    "ReconciliationError",
    "freeze",
    "frozen_paths",
    "load_frozen_connectors",
    "write_frozen_inventory",
    "original_path",
    "recorded_path",
]


class ReconciliationError(FreezeError):
    """Intent and acquisition do not reconcile: an Analysis without an entry, or an entry without an Analysis."""


@dataclass(frozen=True)
class FrozenPaths:
    """Every file one freeze writes, named after the snapshot."""

    inventory: Path
    provenance: Path
    source_metadata_table: Path
    acquisition_manifest: Path

    @property
    def all(self) -> tuple[Path, ...]:
        return (self.inventory, self.provenance, self.source_metadata_table, self.acquisition_manifest)


def frozen_paths(out_dir: Path, snapshot_id: str) -> FrozenPaths:
    out_dir = Path(out_dir)
    return FrozenPaths(
        inventory=out_dir / f"{snapshot_id}.tsv",
        provenance=out_dir / f"{snapshot_id}.meta.yaml",
        source_metadata_table=out_dir / f"{snapshot_id}.source-metadata.tsv",
        acquisition_manifest=out_dir / f"{snapshot_id}.acquisition-manifest.tsv",
    )


@dataclass(frozen=True)
class FrozenInventory:
    """A reconciled Source Inventory and everything its provenance records."""

    snapshot_id: str
    source_collection_id: str
    frozen_at: str
    selection: Selection
    table: SourceMetadataTable
    manifest: AcquisitionManifest
    selected_ids: frozenset[str]
    rows: tuple[SourceInventoryRow, ...]
    lineage: tuple[tuple[str, Lineage], ...]

    @property
    def unselected_entries(self) -> int:
        return sum(1 for entry in self.manifest.entries if entry.source_analysis_id not in self.selected_ids)

    def frozen_table_rows(self) -> list[Mapping[str, str]]:
        return [row for row in self.table.rows if row["source_analysis_id"] in self.selected_ids]

    def frozen_manifest_rows(self) -> list[Mapping[str, str]]:
        return [row for row in self.manifest.rows if row["source_analysis_id"] in self.selected_ids]


def freeze(
    *,
    snapshot_id: str,
    source_collection_id: str,
    metadata_table: Path,
    acquisition_manifest: Path,
    selection: Selection,
    frozen_at: str | None = None,
) -> FrozenInventory:
    """Reconcile the selected intent against the Acquisition Manifest, or fail loudly."""
    table = load_source_metadata_table(metadata_table)
    manifest = load_acquisition_manifest(acquisition_manifest)
    selected_ids = frozenset(row["source_analysis_id"] for row in select(table, selection))

    unclaimed = sorted(
        entry.source_analysis_id for entry in manifest.entries if entry.source_analysis_id not in table
    )
    no_file = sorted(analysis_id for analysis_id in selected_ids if analysis_id not in manifest)
    problems: list[str] = []
    if no_file:
        problems.append(
            f"{len(no_file)} selected Analysis/Analyses have no Acquisition Manifest entry -- "
            f"no file and no recorded reason: {listed(no_file)}"
        )
    if unclaimed:
        problems.append(
            f"{len(unclaimed)} Acquisition Manifest entry/entries are claimed by no Analysis in the "
            f"Source Metadata Table: {listed(unclaimed)}"
        )
    if problems:
        raise ReconciliationError(
            f"cannot freeze {snapshot_id}: " + "; ".join(problems)
            + f" (table {table.path}, manifest {manifest.path})"
        )

    rows: list[SourceInventoryRow] = []
    lineage: list[tuple[str, Lineage]] = []
    for analysis_id in sorted(selected_ids):
        intent = table.row(analysis_id)
        entry = manifest.entry(analysis_id)
        rows.append(
            SourceInventoryRow(
                analysis_id=analysis_id,
                publication_pmid=intent.get("publication_pmid", ""),
                trait=intent.get("source_trait_label", ""),
                study_design=intent.get("study_design", ""),
                sample_size=intent.get("sample_size", ""),
                readiness_status=entry.status,
                data_url=entry.source_url,
                yaml_url=entry.sidecar_url,
                data_file=entry.location,
                yaml_file=entry.sidecar_location,
                data_bytes=_size_text(entry.bytes),
                yaml_bytes=_size_text(entry.sidecar_bytes),
                sha256=entry.sha256,
                error=entry.error,
            )
        )
        if entry.lineage is not None:
            lineage.append((analysis_id, entry.lineage))

    return FrozenInventory(
        snapshot_id=snapshot_id,
        source_collection_id=source_collection_id,
        frozen_at=frozen_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        selection=selection,
        table=table,
        manifest=manifest,
        selected_ids=selected_ids,
        rows=tuple(rows),
        lineage=tuple(lineage),
    )


def write_frozen_inventory(frozen: FrozenInventory, inventory_path: Path, provenance_path: Path) -> str:
    """Write the frozen connectors, the inventory and, last, its provenance; return the inventory's sha256.

    The frozen connectors sit beside the inventory, named after its stem; the
    provenance, written last, binds all three by sha256.
    """
    inventory_path = Path(inventory_path)
    paths = frozen_paths(inventory_path.parent, inventory_path.name.removesuffix(".tsv"))
    table_text = render_connector(SOURCE_METADATA_TABLE, frozen.table.columns, frozen.frozen_table_rows())
    manifest_text = render_connector(ACQUISITION_MANIFEST, frozen.manifest.columns, frozen.frozen_manifest_rows())
    atomic_write(paths.source_metadata_table, table_text)
    atomic_write(paths.acquisition_manifest, manifest_text)
    frozen_files = {
        "source_metadata_table": _frozen_file(paths.source_metadata_table, table_text),
        "acquisition_manifest": _frozen_file(paths.acquisition_manifest, manifest_text),
    }
    return write_inventory(
        frozen.rows,
        inventory_path,
        Path(provenance_path),
        lambda digest: render_provenance_yaml(frozen, digest, frozen_files),
    )


def recorded_path(path: Path) -> str:
    """A connector original's path as provenance records it (#300).

    Relative to the checkout when the original lies inside it, so a committed
    snapshot names the same file in every checkout; absolute otherwise.
    """
    path = Path(path).absolute()
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def original_path(recorded: str) -> Path:
    """The file a provenance's recorded original path names (:func:`recorded_path`'s inverse)."""
    path = Path(recorded)
    return path if path.is_absolute() else REPO_ROOT / path


def render_provenance_yaml(
    frozen: FrozenInventory, inventory_sha256: str, frozen_files: Mapping[str, Mapping[str, Any]]
) -> str:
    """The provenance sidecar: the Selection, both connectors frozen and original, and the counts preflight checks."""
    lineage_path = pass_lineage_path(frozen.manifest.path)
    document: dict[str, Any] = {
        "snapshot_id": frozen.snapshot_id,
        "source_collection_id": frozen.source_collection_id,
        "frozen_at": frozen.frozen_at,
        "frozen_by": "manifest_generation.freeze",
        "selection": frozen.selection.as_dict(),
        **inventory_counts(frozen.rows),
        "inventory_tsv_sha256": inventory_sha256,
        "source_metadata_table": {
            "frozen": dict(frozen_files["source_metadata_table"]),
            "original": {
                "path": recorded_path(frozen.table.path),
                "sha256": frozen.table.sha256,
                "bytes": frozen.table.bytes,
                "rows": len(frozen.table.rows),
            },
        },
        "acquisition_manifest": {
            "frozen": dict(frozen_files["acquisition_manifest"]),
            "original": {
                "path": recorded_path(frozen.manifest.path),
                "sha256": frozen.manifest.sha256,
                "bytes": frozen.manifest.bytes,
                "rows": len(frozen.manifest.entries),
                "readiness_counts": frozen.manifest.readiness_counts,
                "pass_lineage": (
                    {"path": recorded_path(lineage_path), "sha256": sha256_file(lineage_path)}
                    if lineage_path.is_file()
                    else None
                ),
            },
            "unselected_entries": frozen.unselected_entries,
        },
        "lineage": [
            {"analysis_id": analysis_id, **entry.as_dict()} for analysis_id, entry in frozen.lineage
        ],
    }
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True)


def load_frozen_connectors(provenance_path: Path) -> tuple[SourceMetadataTable, AcquisitionManifest]:
    """Load the connector files a freeze wrote, after checking each against its recorded sha256.

    This is how generation reads a frozen Source Collection: from the frozen
    rows beside the inventory, never from the originals, which may since have
    changed or gone.
    """
    provenance_path = Path(provenance_path)
    if not provenance_path.is_file():
        raise FreezeError(f"frozen Source Inventory provenance not found: {provenance_path}")
    document = yaml.safe_load(provenance_path.read_text(encoding="utf-8"))
    paths: list[Path] = []
    for key in ("source_metadata_table", "acquisition_manifest"):
        try:
            recorded = document[key]["frozen"]
            path = provenance_path.parent / recorded["path"]
            expected = recorded["sha256"]
        except (KeyError, TypeError):
            raise FreezeError(
                f"{provenance_path} records no frozen {key}; it was not written by manifest_generation.freeze"
            ) from None
        if not path.is_file():
            raise FreezeError(f"frozen {key} {path} named in {provenance_path} is missing")
        actual = sha256_file(path)
        if actual != expected:
            raise FreezeError(
                f"frozen {key} {path} has sha256 {actual}, but {provenance_path} records {expected}; "
                "it was edited after the freeze"
            )
        paths.append(path)
    return load_source_metadata_table(paths[0]), load_acquisition_manifest(paths[1])


def _frozen_file(path: Path, text: str) -> dict[str, Any]:
    data = text.encode("utf-8")
    return {
        "path": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "rows": text.count("\n") - 1,
    }


def _size_text(value: int | None) -> str:
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-metadata-table", required=True, type=Path)
    parser.add_argument("--acquisition-manifest", required=True, type=Path)
    parser.add_argument(
        "--selection", required=True, type=Path,
        help="a YAML file with a top-level 'selection' block; a release config qualifies",
    )
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--source-collection-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="writes <snapshot-id>.tsv, .meta.yaml and the frozen connectors here")
    parser.add_argument("--frozen-at", help="ISO-8601 freeze timestamp (default: now, UTC)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    paths = frozen_paths(args.out_dir, args.snapshot_id)
    try:
        existing = [str(path) for path in paths.all if path.exists()]
        if existing:
            raise FreezeError(
                f"{', '.join(existing)} already exists; a frozen Source Inventory is never "
                "overwritten -- a re-freeze is a new snapshot id"
            )
        frozen = freeze(
            snapshot_id=args.snapshot_id,
            source_collection_id=args.source_collection_id,
            metadata_table=args.source_metadata_table,
            acquisition_manifest=args.acquisition_manifest,
            selection=load_selection(args.selection),
            frozen_at=args.frozen_at,
        )
        digest = write_frozen_inventory(frozen, paths.inventory, paths.provenance)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    readiness = counts(row.readiness_status for row in frozen.rows)
    print(f"Frozen Source Inventory {frozen.snapshot_id}")
    for label, path in (("inventory", paths.inventory), ("provenance", paths.provenance),
                        ("table", paths.source_metadata_table), ("manifest", paths.acquisition_manifest)):
        print(f"  {label:<11} {path}")
    print(f"  sha256      {digest}")
    print(f"  selection   {frozen.selection.as_dict()}")
    print(f"  rows        {len(frozen.rows)} ({sum(1 for row in frozen.rows if row.ready)} ready)")
    print("  readiness   " + ", ".join(f"{status}={count}" for status, count in readiness.items()))
    print(f"  unselected  {frozen.unselected_entries} Acquisition Manifest entry/entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
