#!/usr/bin/env python3
"""Focused unit tests for real Analysis-metadata resolution (issue #99).

    pixi run python tests/metadata-resolution/run_tests.py

The production workflow suite (`tests/release-workflow/`) drives resolution end
to end through the Snakemake DAG. This suite exercises
`resources/lib/metadata_resolution.py` directly against tiny generated fixtures,
where the interesting cases are the ones a single release cannot show at once:

* a clean AF profile -> `af_assigned` with proportions;
* an ambiguous mixture -> gated out to `unassigned` (never silently keeping a
  prior label);
* a source with no usable allele frequencies -> the declared
  `source_trusted_no_af` trust is preserved, and effect-scale is skipped;
* the resolution report names exactly the Analyses whose metadata was derived.

Fixtures are written into a temporary directory, so nothing is checked in and
nothing is mutated.
"""
from __future__ import annotations

import csv
import gzip
import io
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from resources.lib.metadata_resolution import resolve_analyses  # noqa: E402

FINNGEN_COLUMNS = ("#chrom", "pos", "ref", "alt", "beta", "sebeta", "af_alt", "rsids")

#: Reference variants, A1-oriented: `alid -> (EUR frequency, AFR frequency)`.
REFERENCE = {
    "1:1000:A:G": (0.30, 0.70),
    "1:2000:A:C": (0.55, 0.45),
    "2:3000:A:T": (0.20, 0.80),
    "2:4000:C:G": (0.65, 0.35),
}

ANALYSES_COLUMNS = (
    "analysis_id", "source_file", "stored_effect_scale", "sample_size",
    "sample_size_kind", "sample_size_scope", "source_reader_capability",
    "source_genome_build", "source_ancestry_label", "assigned_ancestry",
    "ancestry_assignment_method", "original_effect_scale", "original_sd",
    "original_sd_method", "exclude_from_build",
)

CONFIG = {
    "reference_resources": [
        {
            "resource_id": "unit-ancestry-mixture",
            "kind": "ancestry_mixture",
            "location": "{reference_freqs}",
            "fine_group_map": "{reference_groups}",
        }
    ],
    "ancestry_assignment": {
        "enabled": True,
        "reference_resource_id": "unit-ancestry-mixture",
        "maf_floor": 0.0,
        "gates": {"tau": 0.5, "delta": 0.2, "n_min": 2, "residual_max": 0.2},
    },
    "effect_scale_validation": {
        "enabled": True,
        "min_overlap_variants": 2,
        "sd_tolerance": 0.15,
        "warning_multiplier": 2.0,
        "dispersion_max": 0.5,
        "block_on_failure": False,
    },
}

n_checks = 0


def check(condition: bool, message: str) -> None:
    global n_checks
    n_checks += 1
    if not condition:
        raise AssertionError(message)


def write_source(path: Path, rows: list[tuple[str, int, str, str, float, float, str]]) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                writer = csv.writer(text, delimiter="\t", lineterminator="\n")
                writer.writerow(FINNGEN_COLUMNS)
                for chrom, pos, ref, alt, beta, se, af in rows:
                    writer.writerow([chrom, pos, ref, alt, beta, se, af, f"rs{pos}"])


def write_reference(directory: Path) -> tuple[Path, Path]:
    freqs = directory / "ref_freqs.tsv.gz"
    groups = directory / "ancestry_groups.tsv"
    with gzip.open(freqs, "wt", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["alid", "chromosome", "position", "effect_allele", "other_allele", "rsid", "EUR_fine", "AFR_fine"])
        for alid, (eur, afr) in REFERENCE.items():
            chrom, pos, a1, a2 = alid.split(":")
            writer.writerow([alid, chrom, pos, a1, a2, f"rs{pos}", f"{eur:.6g}", f"{afr:.6g}"])
    with groups.open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["group", "super_pop"])
        writer.writerow(["EUR_fine", "EUR"])
        writer.writerow(["AFR_fine", "AFR"])
    return freqs, groups


def rows_for(frequencies: dict[str, float], *, af_present: bool = True) -> list[tuple]:
    """FinnGen rows for the reference ALIDs, with `alt` = the canonical A1."""
    out = []
    for alid, frequency in frequencies.items():
        chrom, pos, a1, a2 = alid.split(":")
        ref, alt = a2, a1
        out.append((chrom, int(pos), ref, alt, 0.02, 0.05, f"{frequency:.6g}" if af_present else ""))
    return out


def analysis_row(analysis_id: str, file_name: str, **overrides: str) -> dict[str, str]:
    row = {
        "analysis_id": analysis_id,
        "source_file": file_name,
        "stored_effect_scale": "sd",
        "sample_size": "10000",
        "sample_size_kind": "total",
        "sample_size_scope": "analysis_level",
        "source_reader_capability": "opengwasdb.finngen-r13",
        "source_genome_build": "GRCh38",
        "source_ancestry_label": "European",
        "assigned_ancestry": "EUR",
        "ancestry_assignment_method": "af_assigned",
        "original_effect_scale": "sd",
        "original_sd": "",
        "original_sd_method": "unavailable",
        "exclude_from_build": "",
    }
    row.update(overrides)
    return row


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="metadata-resolution-") as temp:
        directory = Path(temp)
        freqs, groups = write_reference(directory)
        eur = {alid: eur for alid, (eur, _afr) in REFERENCE.items()}
        mixed = {alid: 0.55 * eur + 0.45 * afr for alid, (eur, afr) in REFERENCE.items()}

        write_source(directory / "CLEAN.gz", rows_for(eur))
        write_source(directory / "MIXED.gz", rows_for(mixed))
        write_source(directory / "NOAF.gz", rows_for(eur, af_present=False))

        rows = [
            analysis_row("CLEAN", "CLEAN.gz"),
            analysis_row("MIXED", "MIXED.gz"),
            analysis_row("NOAF", "NOAF.gz", assigned_ancestry="AFR", ancestry_assignment_method="source_trusted_no_af"),
        ]
        config = {
            **CONFIG,
            "reference_resources": [
                {**entry, "location": str(freqs), "fine_group_map": str(groups)}
                for entry in CONFIG["reference_resources"]
            ],
        }

        result = resolve_analyses(
            rows,
            fieldnames=ANALYSES_COLUMNS,
            source_root=directory,
            config=config,
            repo_root=REPO_ROOT,
        )
        check("ancestry_prop_EUR" in result.fieldnames, "the resolved table has no ancestry-proportion column")

        resolved = {row["analysis_id"]: row for row in result.rows}
        check(resolved["CLEAN"]["assigned_ancestry"] == "EUR", "a clean EUR profile was not assigned EUR")
        check(resolved["CLEAN"]["ancestry_assignment_method"] == "af_assigned", "the clean profile is not af_assigned")
        check(float(resolved["CLEAN"]["ancestry_prop_EUR"]) > 0.9, "the clean profile's EUR proportion is not dominant")

        check(resolved["MIXED"]["assigned_ancestry"] == "", "an ambiguous mixture kept an assigned ancestry")
        check(resolved["MIXED"]["ancestry_assignment_method"] == "unassigned",
              "an ambiguous mixture did not become unassigned")

        check(resolved["NOAF"]["ancestry_assignment_method"] == "source_trusted_no_af",
              "an Analysis with no usable AF lost its source-trusted method")
        check(resolved["NOAF"]["assigned_ancestry"] == "AFR",
              "an Analysis with no usable AF had its declared ancestry cleared")

        # An Analysis whose phenotype SD was not established upstream is given one.
        check(resolved["CLEAN"]["original_sd_method"] == "estimated_from_source_maf",
              "a source-AF phenotype SD was not estimated")
        check(resolved["CLEAN"]["original_sd"], "the estimated phenotype SD is blank")

        check(result.checks["ancestry"] == "passed_with_warnings",
              f"gated-out ancestry should warn, got {result.checks['ancestry']!r}")
        check(result.checks["effect_scale"] == "passed",
              f"effect-scale should pass for a verifiable quantitative Analysis, got {result.checks['effect_scale']!r}")
        check(any("MIXED" in warning and "ancestry gate failed" in warning for warning in result.warnings),
              "the gated-out Analysis is not named in the warnings")
        check(not result.blocked, "a reportable effect-scale result blocked the run")

        derived = {row["analysis_id"] for row in result.report_rows if row["derived_fields"]}
        check(derived == {"CLEAN", "MIXED"}, f"the report names the wrong derived Analyses: {derived}")
        report = {row["analysis_id"]: row for row in result.report_rows}
        check(report["NOAF"]["resolution_status"] == "declared",
              "an Analysis with nothing derived is not marked declared")
        check("ancestry_assignment_method" in report["CLEAN"]["derived_fields"],
              "the derived ancestry method is not reported")
        check(report["CLEAN"]["assigned_ancestry"] == "EUR", "the report does not carry the assigned ancestry")

    print(f"metadata-resolution: {n_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
