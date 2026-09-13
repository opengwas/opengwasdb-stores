#!/usr/bin/env python3
"""Turn FinnGen pilot build evidence into a scale-up recommendation."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from resources.generators.lib.release_yaml import (  # noqa: E402
    merge_validation_yaml,
    read_previous_checks_and_warnings,
    read_tsv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--full-analysis-count", type=int, default=2754)
    return parser.parse_args()


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1000 or unit == "PB":
            return f"{value:.2f} {unit}"
        value /= 1000
    raise AssertionError("unreachable")


def main() -> None:
    args = parse_args()
    if args.full_analysis_count < 1:
        raise SystemExit("--full-analysis-count must be at least 1")
    release_dir = Path(args.release_dir).resolve()
    build_rows = read_tsv(release_dir / "sidecars" / "build_report.tsv")
    sd_rows = read_tsv(release_dir / "sidecars" / "sd_estimation.tsv")
    if len(build_rows) != 1:
        raise SystemExit("build_report.tsv must contain exactly one row")
    build = build_rows[0]
    n_analyses = int(build["n_analyses"])
    n_variants = int(build["n_variants"])
    source_bytes = int(build["source_download_bytes"])
    store_bytes = int(build["store_bytes"])
    full_source_estimate = source_bytes / n_analyses * args.full_analysis_count
    full_dense_cells = n_variants * args.full_analysis_count
    failed_sd = [row["analysis_id"] for row in sd_rows if row.get("status") == "failed"]
    checks, warnings = read_previous_checks_and_warnings(release_dir / "validation.yaml")
    query_ok = (
        int(build.get("binary_probe_n_finite_z", 0)) > 0
        and int(build.get("quantitative_probe_n_finite_z", 0)) > 0
    )
    no_go_reasons = []
    if build.get("store_validation_status") != "passed":
        no_go_reasons.append("OpenGWASDB Store validation did not pass")
    if n_analyses != 20:
        no_go_reasons.append(f"built Analysis count is {n_analyses}, not 20")
    if not query_ok:
        no_go_reasons.append("binary and quantitative query probes were not both finite")
    for check, status in checks.items():
        if status in {"failed", "not_run"}:
            no_go_reasons.append(f"required release check {check}={status}")
    if failed_sd:
        no_go_reasons.append(
            "effect-scale evidence failed for " + ", ".join(failed_sd)
        )
    recommendation = "NO-GO" if no_go_reasons else "GO"

    risks = [
        (
            f"Linear source-volume extrapolation is {human_bytes(full_source_estimate)} "
            f"for {args.full_analysis_count:,} endpoints; confirm provider and local capacity."
        ),
        (
            f"A full dense axis at the observed {n_variants:,} variants would contain "
            f"{full_dense_cells:,} variant-by-Analysis cells before considering array fields, "
            "compression, indexes, or staging overhead."
        ),
        (
            "OpenGWASDB's union-axis Pass 1 is intentionally serial, so build wall time will "
            "not scale only with the configured Pass 2 worker count."
        ),
    ]
    if failed_sd:
        risks.insert(
            0,
            "Resolve the quantitative-trait scale discrepancy before treating every IRN "
            "endpoint as declared-standardised.",
        )

    report_lines = [
        "# FinnGen R13 20-Analysis pilot assessment",
        "",
        "## Observed build",
        "",
        f"- Analyses: {n_analyses}",
        f"- GRCh38 union variants: {n_variants:,}",
        f"- Source downloads: {human_bytes(source_bytes)}",
        f"- Store size: {human_bytes(store_bytes)}",
        f"- Build wall time: {float(build['build_wall_seconds']):.1f} seconds",
        f"- Peak observed RSS: {human_bytes(int(build['peak_rss_kib']) * 1024)}",
        f"- Store validation: {build['store_validation_status']}",
        (
            f"- Binary query probe: {build['binary_probe_analysis_id']} "
            f"({int(build['binary_probe_n_finite_z']):,} finite associations)"
        ),
        (
            f"- Quantitative query probe: {build['quantitative_probe_analysis_id']} "
            f"({int(build['quantitative_probe_n_finite_z']):,} finite associations)"
        ),
        "",
        "## Release evidence",
        "",
        f"- Checks: {', '.join(f'{key}={value}' for key, value in checks.items())}",
        f"- Failed SD analyses: {', '.join(failed_sd) if failed_sd else 'none'}",
        f"- Named warnings: {len(warnings)}",
        "",
        "## Scale-up risks",
        "",
        *(f"- {risk}" for risk in risks),
        "",
        "## Decision",
        "",
        *(
            ["Blocking reasons:", "", *(f"- {reason}" for reason in no_go_reasons), ""]
            if no_go_reasons else []
        ),
        (
            "The native FinnGen build/query path is operational, but the complete R13 "
            "collection should not be onboarded until the blocking evidence is resolved."
            if recommendation == "NO-GO"
            else "The pilot evidence supports proceeding to the next bounded scale-up stage."
        ),
        "",
        f"Recommendation: **{recommendation}**",
        "",
    ]
    report_path = release_dir / "pilot_report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name="resources/generators/lib/source-formats/finngen-r13-dense/assess.py",
        updated_checks={},
        updated_reports={"pilot_assessment": "pilot_report.md"},
        new_warnings=[],
    )
    print(f"FinnGen pilot recommendation: {recommendation} ({report_path})")


if __name__ == "__main__":
    main()
