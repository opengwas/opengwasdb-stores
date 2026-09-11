#!/usr/bin/env python3
"""Regenerate the tiny FinnGen R13-shaped fixture release (issue #98).

The fixture is a genuine, if tiny, FinnGen R13 source: three bgzip-compatible
`.gz` tabular files in the shape `opengwasdb.readers.finngen.FinnGenR13Reader`
reads (`#chrom`, `pos`, `ref`, `alt`, `beta`, `sebeta`, `af_alt`, `rsids`), and
the `analyses.tsv` that selects them under the shared Analysis schema.

It is checked in -- three ~200-byte files and one TSV -- so the workflow can be
run against it without any acquisition step. This script exists because
`analyses.tsv` declares each source file's sha256: regenerating the sources
without regenerating their checksums would make the fixture unbuildable.

Run from the repository root:

    python3 tests/release-workflow/fixtures/generate_r13_fixture.py

Gzip headers carry no timestamp (`mtime=0`) and rows are written in a fixed
order, so regenerating is byte-reproducible.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "r13-fixture"
SOURCE_DIR = FIXTURE_DIR / "source"

#: FinnGen R13 summary-statistics columns this fixture reproduces. `#chrom` is
#: FinnGen's own spelling (chromosome 23 is X); `alt` is the effect allele.
FINNGEN_COLUMNS = ("#chrom", "pos", "ref", "alt", "beta", "sebeta", "af_alt", "rsids")

#: `analysis_id` -> (file name, analysis row, variants). Quantitative endpoints
#: are declared-standardised inverse-rank-normalised traits (`sd`,
#: `declared_standardised`, no `original_sd`); the binary endpoint is a
#: case-control `log_or` (`binary_trait`). Both tiers are what the shared Dense
#: builder's manifest validation distinguishes (opengwasdb issues #17/#18).
ENDPOINTS: dict[str, dict] = {
    "finngen-r13-BMI_FIXTURE": {
        "file_name": "finngen_R13_BMI_FIXTURE.gz",
        "source_analysis_id": "BMI_FIXTURE",
        "analysis_label": "Body mass index, inverse-rank normalized",
        "stored_effect_scale": "sd",
        "original_effect_scale": "sd",
        "original_sd_method": "declared_standardised",
        "sample_size_kind": "total",
        "sample_size": "362216",
        "n_cases": "",
        "n_controls": "",
        "variants": [
            ("1", 1000000, "G", "A", 0.12, 0.03, 0.41, "rs1"),
            ("1", 2000000, "T", "C", -0.08, 0.04, 0.33, "rs2"),
            ("2", 3000000, "A", "G", 0.05, 0.02, 0.55, "rs3"),
            ("3", 4000000, "C", "T", 0.07, 0.03, 0.28, "rs4"),
        ],
    },
    "finngen-r13-HEIGHT_FIXTURE": {
        "file_name": "finngen_R13_HEIGHT_FIXTURE.gz",
        "source_analysis_id": "HEIGHT_FIXTURE",
        "analysis_label": "Height, inverse-rank normalized",
        "stored_effect_scale": "sd",
        "original_effect_scale": "sd",
        "original_sd_method": "declared_standardised",
        "sample_size_kind": "total",
        "sample_size": "364515",
        "n_cases": "",
        "n_controls": "",
        "variants": [
            ("1", 1000000, "G", "A", 0.20, 0.05, 0.40, "rs1"),
            ("2", 3000000, "A", "G", -0.10, 0.03, 0.56, "rs3"),
            ("5", 5000000, "A", "T", 0.09, 0.02, 0.22, "rs5"),
            ("6", 6000000, "G", "C", -0.06, 0.02, 0.64, "rs6"),
        ],
    },
    "finngen-r13-RX_STATIN_FIXTURE": {
        "file_name": "finngen_R13_RX_STATIN_FIXTURE.gz",
        "source_analysis_id": "RX_STATIN_FIXTURE",
        "analysis_label": "Statin medication purchase",
        "stored_effect_scale": "log_or",
        "original_effect_scale": "log_or",
        "original_sd_method": "binary_trait",
        "sample_size_kind": "case_control",
        "sample_size": "500186",
        "n_cases": "434037",
        "n_controls": "66149",
        "variants": [
            ("1", 1000000, "G", "A", 0.15, 0.04, 0.42, "rs1"),
            ("4", 7000000, "T", "G", -0.22, 0.06, 0.19, "rs7"),
            ("7", 8000000, "C", "A", 0.11, 0.05, 0.31, "rs8"),
        ],
    },
}

#: The analyses.tsv column order. A superset of the shared Analysis schema's
#: core columns: the extra `checksum`/`checksum_algorithm` columns are what the
#: fixed-input phase verifies.
ANALYSES_COLUMNS = (
    "analysis_id",
    "source_analysis_id",
    "analysis_label",
    "trait_ontology_id",
    "trait_ontology_label",
    "trait_ontology_mapping_method",
    "source_file",
    "checksum",
    "checksum_algorithm",
    "source_reader_capability",
    "source_genome_build",
    "license",
    "consortium",
    "source_ancestry_label",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "original_effect_scale",
    "original_sd",
    "original_sd_method",
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "n_cases",
    "n_controls",
    "exclude_from_build",
)


def write_source(path: Path, variants: list[tuple]) -> None:
    """Write one FinnGen-shaped `.gz` source with a timestamp-free header."""
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                writer = csv.writer(text, delimiter="\t", lineterminator="\n")
                writer.writerow(FINNGEN_COLUMNS)
                for chrom, pos, ref, alt, beta, se, af, rsid in variants:
                    writer.writerow([chrom, pos, ref, alt, beta, se, af, rsid])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    for entry in ENDPOINTS.values():
        write_source(SOURCE_DIR / entry["file_name"], entry["variants"])

    with (FIXTURE_DIR / "analyses.tsv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANALYSES_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for analysis_id, entry in ENDPOINTS.items():
            source_file = SOURCE_DIR / entry["file_name"]
            writer.writerow({
                "analysis_id": analysis_id,
                "source_analysis_id": entry["source_analysis_id"],
                "analysis_label": entry["analysis_label"],
                "trait_ontology_id": "",
                "trait_ontology_label": "",
                "trait_ontology_mapping_method": "unmapped",
                "source_file": entry["file_name"],
                "checksum": sha256(source_file),
                "checksum_algorithm": "sha256",
                "source_reader_capability": "opengwasdb.finngen-r13",
                "source_genome_build": "GRCh38",
                "license": "FinnGen public data",
                "consortium": "FinnGen",
                "source_ancestry_label": "Finnish",
                "assigned_ancestry": "EUR",
                "ancestry_assignment_method": "af_assigned",
                "original_effect_scale": entry["original_effect_scale"],
                "original_sd": "",
                "original_sd_method": entry["original_sd_method"],
                "stored_effect_scale": entry["stored_effect_scale"],
                "sample_size_kind": entry["sample_size_kind"],
                "sample_size_scope": "analysis_level",
                "sample_size": entry["sample_size"],
                "n_cases": entry["n_cases"],
                "n_controls": entry["n_controls"],
                "exclude_from_build": "",
            })


if __name__ == "__main__":
    main()
