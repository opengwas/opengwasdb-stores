#!/usr/bin/env python3
"""CLI runner for the QC Panel Concordance Study (issue #152).

Executes Method A (Full Reference Scan) vs Method B (QC Panel Extraction)
across the preregistered stratified sample manifest and outputs machine-readable
evidence and a summary Markdown report.

Usage:
  python3 resources/generators/gwas-catalog-eur-hybrid/run_concordance_study.py \
    --manifest resources/inventories/gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.tsv \
    --config resources/generators/gwas-catalog-eur-hybrid/config-full.yaml \
    --cores 64 \
    --out-dir /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Ensure opengwasdb is discoverable
for sibling_candidate in [
    Path("/home/gh13047/repo/opengwasdb-phase-b-integration"),
    Path("/home/gh13047/repo/opengwasdb-ticket207"),
    Path("/home/gh13047/repo/opengwasdb"),
]:
    if sibling_candidate.exists() and (sibling_candidate / "opengwasdb" / "build" / "resolve.py").exists():
        if str(sibling_candidate) not in sys.path:
            sys.path.insert(0, str(sibling_candidate))
        break

from resources.generators.lib.qc_panel_concordance import (
    COMPARISON_TSV_COLUMNS,
    evaluate_concordance_study,
    render_concordance_markdown_report,
)

DEFAULT_MANIFEST = REPO_ROOT / "resources" / "inventories" / "gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.tsv"
DEFAULT_CONFIG = REPO_ROOT / "resources" / "generators" / "gwas-catalog-eur-hybrid" / "config-full.yaml"
DEFAULT_OUT_DIR = Path("/data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Sample manifest TSV")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Phase B full config YAML")
    parser.add_argument("--cores", type=int, default=1, help="Concurrent worker count (up to 64)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory for reports")
    parser.add_argument("--max-analyses", type=int, default=None, help="Cap number of analyses to evaluate")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print("=" * 70)
    print("QC Panel Concordance Study Runner (#152)")
    print(f"Manifest: {args.manifest}")
    print(f"Config:   {args.config}")
    print(f"Cores:    {args.cores}")
    print(f"Out Dir:  {args.out_dir}")
    print("=" * 70)

    summary = evaluate_concordance_study(
        manifest_path=args.manifest,
        config_path=args.config,
        cores=args.cores,
        max_analyses=args.max_analyses,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "concordance_results.json"
    tsv_path = args.out_dir / "concordance_comparison.tsv"
    md_path = args.out_dir / "concordance_report.md"

    # 1. Write JSON Results
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "summary": {
                    "created_at": summary.created_at,
                    "sample_manifest_path": summary.sample_manifest_path,
                    "config_path": summary.config_path,
                    "total_analyses": summary.total_analyses,
                    "quantitative_count": summary.quantitative_count,
                    "case_control_count": summary.case_control_count,
                    "ancestry_concordance_rate": summary.ancestry_concordance_rate,
                    "dominant_superpop_concordance_rate": summary.dominant_superpop_concordance_rate,
                    "gate_reason_concordance_rate": summary.gate_reason_concordance_rate,
                    "false_positive_eur_count": summary.false_positive_eur_count,
                    "orientation_sensitivity_rate": summary.orientation_sensitivity_rate,
                    "total_disagreements": summary.total_disagreements,
                    "disagreement_counts": summary.disagreement_counts,
                    "mean_full_seconds": summary.mean_full_seconds,
                    "mean_panel_seconds": summary.mean_panel_seconds,
                    "speedup_ratio": summary.speedup_ratio,
                    "criteria_evaluation": summary.criteria_evaluation,
                    "recommendation": summary.recommendation,
                },
                "results": [r.as_dict() for r in summary.results],
            },
            fh,
            indent=2,
        )

    # 2. Write TSV Comparison Table
    with tsv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COMPARISON_TSV_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for r in summary.results:
            writer.writerow(r.to_comparison_tsv_row())

    # 3. Write Markdown Report
    report_md = render_concordance_markdown_report(summary)
    md_path.write_text(report_md, encoding="utf-8")

    print("\n" + report_md)
    print("=" * 70)
    print(f"Complete results written to:")
    print(f"  - JSON: {json_path}")
    print(f"  - TSV:  {tsv_path}")
    print(f"  - MD:   {md_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
