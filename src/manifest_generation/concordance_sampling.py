"""Deterministic stratified sampling for the QC Panel Concordance Study (issue #152).

Derives a frozen representative sample manifest from the frozen GWAS Catalog
Source Inventory (issue #151). The sample frame covers:
- 10 file-size deciles for quantitative traits (5 Analyses per decile = 50 rows)
- 10 file-size deciles for case-control traits (5 Analyses per decile = 50 rows)
- Explicit edge-case and anomaly fixtures:
  - GCST90446781 (known inverted-EAF / orientation failure)
  - GCST000553 (unpopulated frequency rows / older header layout)
  - GCST90271757 (small case-control accession)
  - GCST90565871 & GCST90565872 (duplicate content pair 1)
  - GCST90624704 & GCST90624705 (duplicate content pair 2)

Total sample: 106 Analyses.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import yaml

from resources.generators.lib.source_inventory import (
    INVENTORY_COLUMNS,
    READY_STATUSES,
    SourceInventoryRow,
    read_inventory,
    sha256_file,
)

SAMPLE_COLUMNS: tuple[str, ...] = INVENTORY_COLUMNS + ("stratum",)

EXPLICIT_EDGE_CASE_IDS: tuple[str, ...] = (
    "GCST90446781",  # Inverted EAF / orientation failure
    "GCST000553",    # Unpopulated NA frequency rows / prefix header
    "GCST90271757",  # Small case-control accession (248 KB)
    "GCST90565871",  # Duplicate content group 1 member A
    "GCST90565872",  # Duplicate content group 1 member B
    "GCST90624704",  # Duplicate content group 2 member A
    "GCST90624705",  # Duplicate content group 2 member B
)


@dataclass(frozen=True)
class SampledAnalysisRow:
    """One sampled Analysis row with its stratification assignment."""

    inventory_row: SourceInventoryRow
    stratum: str

    @property
    def analysis_id(self) -> str:
        return self.inventory_row.analysis_id

    def as_tsv_row(self) -> dict[str, str]:
        row = self.inventory_row.as_dict()
        row["stratum"] = self.stratum
        return row


def _sample_deciles(
    rows: Sequence[SourceInventoryRow],
    prefix: str,
    per_decile: int = 5,
) -> list[SampledAnalysisRow]:
    """Sample `per_decile` rows evenly from each of 10 size deciles."""
    n = len(rows)
    if n == 0:
        return []
    sampled: list[SampledAnalysisRow] = []
    for d in range(10):
        start = (d * n) // 10
        end = ((d + 1) * n) // 10
        decile_rows = rows[start:end]
        sub_n = len(decile_rows)
        if sub_n == 0:
            continue
        # Evenly spaced relative ranks
        ranks = [0.0, 0.25, 0.50, 0.75, 1.0] if per_decile == 5 else [
            i / (per_decile - 1) for i in range(per_decile)
        ]
        indices = [int(round(r * (sub_n - 1))) for r in ranks]
        seen_idx: set[int] = set()
        for idx in indices:
            if idx not in seen_idx:
                seen_idx.add(idx)
                sampled.append(
                    SampledAnalysisRow(
                        inventory_row=decile_rows[idx],
                        stratum=f"{prefix}_decile_{d+1}",
                    )
                )
    return sampled


def build_concordance_sample(
    inventory_rows: Sequence[SourceInventoryRow],
    quant_per_decile: int = 5,
    cc_per_decile: int = 5,
    explicit_ids: Sequence[str] = EXPLICIT_EDGE_CASE_IDS,
) -> list[SampledAnalysisRow]:
    """Build the deterministic stratified sample for the concordance study."""
    ready_rows = [r for r in inventory_rows if r.ready]
    by_id = {r.analysis_id: r for r in ready_rows}

    # Partition by study design and sort by data_bytes ascending
    quant_rows = sorted(
        [r for r in ready_rows if r.study_design == "quantitative"],
        key=lambda r: (r.recorded_bytes or 0, r.analysis_id),
    )
    cc_rows = sorted(
        [r for r in ready_rows if r.study_design == "case-control"],
        key=lambda r: (r.recorded_bytes or 0, r.analysis_id),
    )

    quant_sample = _sample_deciles(quant_rows, "quant", per_decile=quant_per_decile)
    cc_sample = _sample_deciles(cc_rows, "case_control", per_decile=cc_per_decile)

    selected_by_id: dict[str, SampledAnalysisRow] = {}
    for s in quant_sample:
        selected_by_id[s.analysis_id] = s
    for s in cc_sample:
        selected_by_id[s.analysis_id] = s

    # Add explicit edge cases if present in inventory
    for eid in explicit_ids:
        if eid in by_id:
            if eid not in selected_by_id:
                selected_by_id[eid] = SampledAnalysisRow(
                    inventory_row=by_id[eid],
                    stratum="explicit_edge_case",
                )

    # Sort deterministically by analysis_id
    return sorted(selected_by_id.values(), key=lambda s: s.analysis_id)


def write_sample_manifest(
    sample_rows: Sequence[SampledAnalysisRow],
    output_tsv: Path,
    output_meta: Path,
    inventory_path: Path,
    snapshot_id: str,
) -> None:
    """Write the sample TSV and its provenance sidecar."""
    output_tsv.parent.mkdir(parents=True, exist_ok=True)
    output_meta.parent.mkdir(parents=True, exist_ok=True)

    # 1. Write TSV
    temp_tsv = output_tsv.with_suffix(".tmp")
    with temp_tsv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SAMPLE_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for s in sample_rows:
            writer.writerow(s.as_tsv_row())
    temp_tsv.replace(output_tsv)

    # 2. Build metadata provenance
    tsv_hash = sha256_file(output_tsv)
    tsv_bytes = output_tsv.stat().st_size
    inventory_hash = sha256_file(inventory_path)

    strata_counts: dict[str, int] = {}
    for s in sample_rows:
        strata_counts[s.stratum] = strata_counts.get(s.stratum, 0) + 1

    study_design_counts: dict[str, int] = {}
    for s in sample_rows:
        sd = s.inventory_row.study_design
        study_design_counts[sd] = study_design_counts.get(sd, 0) + 1

    provenance = {
        "sample_snapshot_id": snapshot_id,
        "source_inventory": {
            "path": "resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv",
            "sha256": inventory_hash,
        },
        "created_at": "2026-09-21T00:00:00Z",
        "total_sample_analyses": len(sample_rows),
        "study_design_counts": study_design_counts,
        "strata_counts": strata_counts,
        "explicit_edge_cases": [s.analysis_id for s in sample_rows if s.stratum == "explicit_edge_case"],
        "sample_tsv_sha256": tsv_hash,
        "sample_tsv_bytes": tsv_bytes,
    }

    temp_meta = output_meta.with_suffix(".tmp")
    with temp_meta.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(provenance, fh, sort_keys=False)
    temp_meta.replace(output_meta)
