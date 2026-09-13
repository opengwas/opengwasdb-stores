#!/usr/bin/env python3
"""Pilot assessment turns physical evidence into a go/no-go decision."""
from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_raw:
        release = Path(tmp_raw)
        write_tsv(
            release / "sidecars" / "downloads.tsv",
            [
                {"analysis_id": f"finngen-r13-{index}", "size_bytes": 750_000_000}
                for index in range(20)
            ],
        )
        write_tsv(
            release / "sidecars" / "build_report.tsv",
            [{
                "n_analyses": 20,
                "n_variants": 10_000_000,
                "source_download_bytes": 15_000_000_000,
                "store_bytes": 2_000_000_000,
                "build_wall_seconds": 1200,
                "peak_rss_kib": 4_000_000,
                "binary_probe_analysis_id": "finngen-r13-BINARY",
                "binary_probe_n_finite_z": 9_000_000,
                "quantitative_probe_analysis_id": "finngen-r13-HEIGHT_IRN",
                "quantitative_probe_n_finite_z": 9_000_000,
                "store_validation_status": "passed",
            }],
        )
        write_tsv(
            release / "sidecars" / "sd_estimation.tsv",
            [
                {"analysis_id": "finngen-r13-BMI_IRN", "status": "passed"},
                {"analysis_id": "finngen-r13-HEIGHT_IRN", "status": "failed"},
                {"analysis_id": "finngen-r13-WEIGHT_IRN", "status": "passed"},
            ],
        )
        (release / "validation.yaml").write_text(
            "checks:\n  schema: passed\n  files: passed\n  effect_scale: failed\n"
            "  store: passed\nreports:\n  build_report: sidecars/build_report.tsv\n"
            "warnings:\n  - finngen-r13-HEIGHT_IRN: empirical effect-scale status=failed\n"
            "errors: []\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                "resources/generators/lib/source-formats/finngen-r13-dense/assess.py",
                f"--release-dir={release}",
                "--full-analysis-count=2754",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = (release / "pilot_report.md").read_text(encoding="utf-8")
        assert "15.00 GB" in report
        assert "2.07 TB" in report
        assert "finngen-r13-HEIGHT_IRN" in report
        assert report.rstrip().endswith("Recommendation: **NO-GO**")
        validation = (release / "validation.yaml").read_text(encoding="utf-8")
        assert "pilot_assessment: pilot_report.md" in validation

        # A failed required release check blocks GO even when Store, probes,
        # and scientific SD evidence are otherwise clean.
        write_tsv(
            release / "sidecars" / "sd_estimation.tsv",
            [
                {"analysis_id": "finngen-r13-BMI_IRN", "status": "passed"},
                {"analysis_id": "finngen-r13-HEIGHT_IRN", "status": "passed"},
                {"analysis_id": "finngen-r13-WEIGHT_IRN", "status": "passed"},
            ],
        )
        (release / "validation.yaml").write_text(
            "checks:\n  schema: failed\n  files: passed\n  effect_scale: passed\n"
            "  store: passed\nreports:\n  build_report: sidecars/build_report.tsv\n"
            "warnings: []\nerrors:\n  - shared schema rejected the manifest\n",
            encoding="utf-8",
        )
        failed_check = subprocess.run(
            [
                sys.executable,
                "resources/generators/lib/source-formats/finngen-r13-dense/assess.py",
                f"--release-dir={release}",
                "--full-analysis-count=2754",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert failed_check.returncode == 0, failed_check.stdout + failed_check.stderr
        failed_report = (release / "pilot_report.md").read_text(encoding="utf-8")
        assert "required release check schema=failed" in failed_report
        assert failed_report.rstrip().endswith("Recommendation: **NO-GO**")
    print("ALL 9 CHECKS PASSED")


if __name__ == "__main__":
    main()
