#!/usr/bin/env python3
"""Write the GWAS Catalog Acquisition Manifest from the EBI download passes (#283, ADR 0036).

The GWAS Catalog downloader (``download_ebi_gwas_catalog_eur_hybrid.py``)
writes one status manifest per acquisition pass: a base pass, a retry of its
transient failures, a rescue through EBI's harmonised index. Overlaying them is
acquisition's business, so it happens here, once, with the rule the
candidate-table freeze applied (``source_inventory``): passes are given
earliest first, a later pass is authoritative for every ``analysis_id`` it
covers, and a later pass may not turn a ready Analysis into a non-ready one.

The result is one canonical Acquisition Manifest (``acquisition_manifest``):
per Analysis, its harmonised data file (``location``, ``bytes``, ``sha256``,
``source_url``), its ``-meta.yaml`` (the sidecar columns), its readiness
``status`` and ``error``. The download manifests' study columns
(``publication_pmid``, ``trait``, ``study_design``, ``sample_size``) are intent,
not availability, and are left to the Source Metadata Table; ``seconds`` is
per-run timing and is dropped. A sidecar ``<out-stem>.meta.yaml`` records each
pass's path, sha256, size, row count, readiness counts and overrides, so the
manifest traces back to the passes it was written from.

Usage:
    python3 -m manifest_generation.providers.gwas_catalog.write_acquisition_manifest \\
        --config resources/generators/gwas-catalog-eur-hybrid/config-full.yaml --out <manifest.tsv>
    python3 -m manifest_generation.providers.gwas_catalog.write_acquisition_manifest \\
        --pass base=<manifest.tsv> --pass retry_transient=<manifest.tsv> --out <manifest.tsv>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.source_inventory import (
    AcquisitionPass,
    InventoryError,
    atomic_write,
    counts,
    merge_acquisition_manifests,
    parse_freeze_inputs,
    parse_pass_arguments,
    read_acquisition_pass,
    reject_ready_regressions,
    sha256_file,
)

#: Download-manifest column -> canonical Acquisition Manifest column.
COLUMN_MAP: tuple[tuple[str, str], ...] = (
    ("analysis_id", "source_analysis_id"),
    ("status", "status"),
    ("data_file", "location"),
    ("data_bytes", "bytes"),
    ("sha256", "sha256"),
    ("data_url", "source_url"),
    ("yaml_file", "sidecar_location"),
    ("yaml_url", "sidecar_url"),
    ("yaml_bytes", "sidecar_bytes"),
    ("error", "error"),
)


def write_from_passes(passes: Sequence[AcquisitionPass], out: Path) -> Path:
    """Overlay ``passes`` (earliest first) into one Acquisition Manifest; return its sidecar path."""
    if not passes:
        raise InventoryError("an Acquisition Manifest needs at least one download pass")
    read = [(entry.role, read_acquisition_pass(entry.path)) for entry in passes]
    merged, overrides = merge_acquisition_manifests(read)
    reject_ready_regressions(read, {row["analysis_id"]: row for row in merged})
    out = Path(out)
    write_connector(
        out,
        ACQUISITION_MANIFEST,
        [canonical for _, canonical in COLUMN_MAP],
        [{canonical: row[column] for column, canonical in COLUMN_MAP} for row in merged],
    )
    try:
        manifest = load_acquisition_manifest(out)
    except InventoryError:
        out.unlink()
        raise
    sidecar = pass_lineage_path(out)
    document: dict[str, Any] = {
        "acquisition_manifest": str(out),
        "sha256": manifest.sha256,
        "rows": len(manifest.entries),
        "readiness_counts": manifest.readiness_counts,
        "written_by": "manifest_generation.providers.gwas_catalog.write_acquisition_manifest",
        "overlay_rule": "passes earliest first; a later pass wins for every analysis_id it covers "
        "and may not regress a ready Analysis",
        "passes": [
            {
                "role": entry.role,
                "path": str(entry.path),
                "sha256": sha256_file(entry.path),
                "bytes": entry.path.stat().st_size,
                "rows": len(rows),
                "readiness_counts": counts(row["status"] for row in rows),
                "overrides": overrides[entry.role],
            }
            for entry, (_, rows) in zip(passes, read)
        ],
    }
    atomic_write(sidecar, yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100))
    return sidecar


def passes_from_config(path: Path) -> tuple[AcquisitionPass, ...]:
    """The ordered passes a candidate-workflow config declares in ``source.inventory.freeze_inputs``."""
    document: Mapping[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    try:
        raw = document["source"]["inventory"]["freeze_inputs"]
    except (KeyError, TypeError):
        raise InventoryError(f"{path} declares no source.inventory.freeze_inputs") from None
    return parse_freeze_inputs(raw, Path(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, help="a config whose source.inventory.freeze_inputs lists the passes")
    source.add_argument("--pass", dest="passes", action="append", metavar="ROLE=PATH",
                        help="one download pass; repeatable, earliest first")
    parser.add_argument("--out", required=True, type=Path, help="the Acquisition Manifest to write")
    args = parser.parse_args(argv)
    try:
        passes = passes_from_config(args.config) if args.config else parse_pass_arguments(args.passes, flag="--pass")
        sidecar = write_from_passes(passes, args.out)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Acquisition Manifest {args.out} written from {len(passes)} pass(es); provenance {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
