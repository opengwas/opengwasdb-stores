#!/usr/bin/env python3
"""Deterministically (re)generates the tiny ancestry-mixture reference + release
fixtures used by tests/opengwas-gwas-vcf-dense/test_annotation.py (see
../README.md).

Run from the repository root: python3 tests/ancestry-assignment/fixtures/generate_fixtures.py
"""
from __future__ import annotations

import csv
import gzip
from pathlib import Path

FIXTURES_DIR = Path("tests/ancestry-assignment/fixtures")
REFERENCE_DIR = FIXTURES_DIR / "reference"
RELEASE_DIR = FIXTURES_DIR / "release"
FILTERED_DIR = RELEASE_DIR / "filtered"

# Four fine groups, one per super-population, ten diagnostic SNPs each: high
# frequency (0.5) in the group they diagnose, low (0.05) everywhere else. A
# study whose AF profile matches one fine group's column almost exactly
# should therefore fit an NNLS mixture dominated by that one super-population.
GROUPS = ["EUR_fine", "AFR_fine", "EAS_fine", "SAS_fine"]
SUPERPOP_OF = {"EUR_fine": "EUR", "AFR_fine": "AFR", "EAS_fine": "EAS", "SAS_fine": "SAS"}
N_PER_BLOCK = 10
HIGH, LOW = 0.5, 0.05


def build_reference() -> dict[str, dict[str, float]]:
    """Return {alid: {group: freq}} for the whole synthetic panel."""
    rows: dict[str, dict[str, float]] = {}
    bp = 1000
    for block_group in GROUPS:
        for _ in range(N_PER_BLOCK):
            alid = f"1:{bp}:A:G"
            rows[alid] = {g: (HIGH if g == block_group else LOW) for g in GROUPS}
            bp += 10
    return rows


def write_reference(rows: dict[str, dict[str, float]]) -> None:
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(REFERENCE_DIR / "ref_freqs.hg38.tsv.gz", "wt", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(["alid", "chromosome", "position", "effect_allele", "other_allele", "rsid", *GROUPS])
        for alid, freqs in rows.items():
            _chrom, pos, ea, oa = alid.split(":")
            writer.writerow([alid, _chrom, pos, ea, oa, f"rs{pos}", *[f"{freqs[g]:.4g}" for g in GROUPS]])
    with open(REFERENCE_DIR / "ancestry_groups.tsv", "w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(["group", "super_pop"])
        for g in GROUPS:
            writer.writerow([g, SUPERPOP_OF[g]])


def alid_to_ssf_row(alid: str, af: float) -> dict[str, str]:
    chrom, pos, ea, oa = alid.split(":")
    return {
        "chromosome": chrom, "base_pair_location": pos, "effect_allele": ea, "other_allele": oa,
        "beta": "0.01", "standard_error": "0.05", "p_value": "0.5",
        "effect_allele_frequency": f"{af:.4g}", "variant_id": f"rs{pos}",
    }


def write_ssf(filename: str, rows: list[dict[str, str]]) -> None:
    FILTERED_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = ["chromosome", "base_pair_location", "effect_allele", "other_allele",
                  "beta", "standard_error", "p_value", "effect_allele_frequency", "variant_id"]
    with gzip.open(FILTERED_DIR / filename, "wt", newline="") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def base_analysis_row(analysis_id: str, source_ancestry_label: str, filtered_file: str) -> dict[str, str]:
    return {
        "analysis_index": "0", "analysis_id": analysis_id, "source_analysis_id": analysis_id,
        "source_label": f"Fixture analysis {analysis_id}", "trait_ontology_label": "", "trait_ontology_id": "",
        "source_file": f"filtered/{filtered_file}", "filtered_file": filtered_file,
        "source_bundle_id": "", "checksum": "", "checksum_algorithm": "sha256", "size_bytes": "",
        "source_genome_build": "GRCh38", "license": "test-fixture",
        "publication_doi": "", "publication_pmid": "", "consortium": "",
        "source_ancestry_label": source_ancestry_label, "assigned_ancestry": source_ancestry_label,
        "ancestry_assignment_method": "source_trusted_no_af",
        "original_effect_scale": "sd", "original_sd": "", "original_sd_method": "declared_standardised",
        "stored_effect_scale": "sd", "sample_size_kind": "total", "sample_size_scope": "analysis_level",
        "sample_size": "10000", "n_cases": "", "n_controls": "",
        "analysis_group_id": "FIXTURES", "inclusion_reason": "ancestry_assignment_fixture", "exclude_from_build": "",
    }


def main() -> None:
    reference_rows = build_reference()
    write_reference(reference_rows)
    alids = list(reference_rows.keys())

    analyses: list[dict[str, str]] = []

    # FIXT_EUR: matches the EUR fine group column exactly -> af_assigned EUR.
    write_ssf("FIXT_EUR.filtered.tsv.gz", [alid_to_ssf_row(a, reference_rows[a]["EUR_fine"]) for a in alids])
    analyses.append(base_analysis_row("FIXT_EUR", "European", "FIXT_EUR.filtered.tsv.gz"))

    # FIXT_AFR: matches the AFR fine group column exactly -> af_assigned AFR.
    write_ssf("FIXT_AFR.filtered.tsv.gz", [alid_to_ssf_row(a, reference_rows[a]["AFR_fine"]) for a in alids])
    analyses.append(base_analysis_row("FIXT_AFR", "African", "FIXT_AFR.filtered.tsv.gz"))

    # FIXT_MIXED: a 55/45 EUR/SAS blend at every site. An exact 50/50 blend
    # would put the fitted dominant proportion right on the tau=0.50 gate
    # boundary, where sub-percent NNLS solver differences across platforms
    # (e.g. different BLAS/LAPACK backends between local dev and CI) can tip
    # gate_reason between "proportion" and "margin". 55/45 keeps
    # dominant_proportion (~0.55) safely above tau=0.50 while keeping margin
    # (~0.10) safely below the default delta=0.20 gate, so this reliably
    # exercises the margin gate specifically, not the proportion gate.
    mixed_rows = [
        alid_to_ssf_row(a, 0.55 * reference_rows[a]["EUR_fine"] + 0.45 * reference_rows[a]["SAS_fine"])
        for a in alids
    ]
    write_ssf("FIXT_MIXED.filtered.tsv.gz", mixed_rows)
    analyses.append(base_analysis_row("FIXT_MIXED", "European", "FIXT_MIXED.filtered.tsv.gz"))

    # FIXT_LOW_OVERLAP: only 3 of the 40 reference sites -> below n_min gate.
    low_overlap_rows = [alid_to_ssf_row(a, reference_rows[a]["EUR_fine"]) for a in alids[:3]]
    write_ssf("FIXT_LOW_OVERLAP.filtered.tsv.gz", low_overlap_rows)
    analyses.append(base_analysis_row("FIXT_LOW_OVERLAP", "European", "FIXT_LOW_OVERLAP.filtered.tsv.gz"))

    # FIXT_NO_AF: AF column present but empty -> skipped, left source_trusted_no_af.
    no_af_rows = [alid_to_ssf_row(a, 0.0) for a in alids]
    for row in no_af_rows:
        row["effect_allele_frequency"] = ""
    write_ssf("FIXT_NO_AF.filtered.tsv.gz", no_af_rows)
    analyses.append(base_analysis_row("FIXT_NO_AF", "European", "FIXT_NO_AF.filtered.tsv.gz"))

    # FIXT_MISMATCH: matches AFR exactly but declares European source ancestry
    # -> af_assigned AFR, source_assigned_mismatch=true.
    write_ssf("FIXT_MISMATCH.filtered.tsv.gz", [alid_to_ssf_row(a, reference_rows[a]["AFR_fine"]) for a in alids])
    analyses.append(base_analysis_row("FIXT_MISMATCH", "European", "FIXT_MISMATCH.filtered.tsv.gz"))

    for i, row in enumerate(analyses):
        row["analysis_index"] = str(i)

    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = list(analyses[0].keys())
    with (RELEASE_DIR / "analyses.tsv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(analyses)

    # ancestry_assign mutates validation.yaml (and analyses.tsv, above) in
    # place. Reset it to a pristine not_run baseline on every regeneration so
    # repeated test runs are idempotent rather than accumulating warnings
    # across invocations.
    (RELEASE_DIR / "validation.yaml").write_text(
        "\n".join([
            "status: not_run",
            "validated_at: ~",
            "validator:",
            "  name: ~",
            "  version: ~",
            "checks:",
            "  schema: not_run",
            "  files: not_run",
            "  reader_smoke_test: not_run",
            "  ancestry: not_run",
            "  effect_scale: not_run",
            "  sd_estimation: not_run",
            "  sparse_regions: not_run",
            "reports: {}",
            "warnings: []",
            "errors: []",
            "",
        ]),
        encoding="utf-8",
    )

    print(f"Wrote {len(analyses)} fixture analyses and {len(alids)} reference variants")


if __name__ == "__main__":
    main()
