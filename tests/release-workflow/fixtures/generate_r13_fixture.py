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

Issue #99 makes the fixture exercise *real* metadata resolution, so the script
also writes the two inputs that resolution reads and the release bundle's
lifecycle record:

* `fixtures/ancestry-reference/` -- a two-fine-group ancestry-mixture panel
  (one EUR group, one AFR group) over exactly the fixture's variants, so an
  AF-based fit assigns EUR. It is deliberately tiny: `maf_floor: 0` and a low
  `gates.n_min` in the release's `build.yaml` keep every variant informative.
* `release.yaml` -- the lifecycle record the workflow lands as `built` or
  `validated` (a failed effect-scale check is `built`, issue #99).

Effect-scale roles are deliberate: `BMI_FIXTURE` declares no upstream phenotype
SD (`original_sd_method: unavailable`), so resolution *derives* one; the two
inverse-rank-normalised traits are seeded so `HEIGHT_FIXTURE` genuinely fails
the declared-standardised check (implied SD ~0.665, mirroring the real R13
pilot) while `BMI_FIXTURE` passes; the binary `RX_STATIN_FIXTURE` is skipped as
non-quantitative.

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

FIXTURES_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = FIXTURES_DIR / "r13-fixture"
SOURCE_DIR = FIXTURE_DIR / "source"
ANCESTRY_REFERENCE_DIR = FIXTURES_DIR / "ancestry-reference"

#: FinnGen R13 summary-statistics columns this fixture reproduces. `#chrom` is
#: FinnGen's own spelling (chromosome 23 is X); `alt` is the effect allele.
FINNGEN_COLUMNS = ("#chrom", "pos", "ref", "alt", "beta", "sebeta", "af_alt", "rsids")

#: Fine ancestry groups in the fixture mixture panel, and the super-population
#: each aggregates to. Two groups keep the NNLS fit well-conditioned on the
#: fixture's handful of variants.
ANCESTRY_GROUPS = (("EUR_fine", "EUR"), ("AFR_fine", "AFR"))
ANCESTRY_REFERENCE_COLUMNS = ("alid", "chromosome", "position", "effect_allele", "other_allele", "rsid")

#: `analysis_id` -> (file name, analysis row, variants). Quantitative endpoints
#: are declared-standardised inverse-rank-normalised traits (`sd`); the binary
#: endpoint is a case-control `log_or` (`binary_trait`). Both tiers are what the
#: shared Dense builder's manifest validation distinguishes (opengwasdb issues
#: #17/#18).
#:
#: `sebeta` magnitudes are seeded against `implied_sd = se * sqrt(2*N*af*(1-af))`
#: (ADR-0029): BMI_FIXTURE's imply SD ~1.0 and HEIGHT_FIXTURE's imply SD ~0.665,
#: which is outside `sd_tolerance` (0.15) and so fails the declared-standardised
#: check the way the real R13 `HEIGHT_IRN` does.
ENDPOINTS: dict[str, dict] = {
    "finngen-r13-BMI_FIXTURE": {
        "file_name": "finngen_R13_BMI_FIXTURE.gz",
        "source_analysis_id": "BMI_FIXTURE",
        "analysis_label": "Body mass index, inverse-rank normalized",
        "stored_effect_scale": "sd",
        "original_effect_scale": "sd",
        "original_sd_method": "unavailable",
        "sample_size_kind": "total",
        "sample_size": "362216",
        "n_cases": "",
        "n_controls": "",
        "variants": [
            ("1", 1000000, "G", "A", 0.12, 0.00239, 0.41, "rs1"),
            ("1", 2000000, "T", "C", -0.08, 0.00250, 0.33, "rs2"),
            ("2", 3000000, "A", "G", 0.05, 0.00236, 0.55, "rs3"),
            ("3", 4000000, "C", "T", 0.07, 0.00262, 0.28, "rs4"),
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
            ("1", 1000000, "G", "A", 0.20, 0.00159, 0.40, "rs1"),
            ("2", 3000000, "A", "G", -0.10, 0.00157, 0.56, "rs3"),
            ("5", 5000000, "A", "T", 0.09, 0.00188, 0.22, "rs5"),
            ("6", 6000000, "G", "C", -0.06, 0.00162, 0.64, "rs6"),
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

RELEASE_YAML = """\
metadata_schema_version: 1.0
store_family_id: finngen-r13
family_release_id: r13-fixture
status: candidate
source_collection_id: finngen-r13
source_snapshot_id: finngen-r13-fixture
release_kind: one-off
association_coverage: full_gwas
description: >
  Tiny FinnGen R13-shaped fixture release for the production Store Release
  workflow (issue #98), extended with real metadata resolution (issue #99).
generator:
  name: tests/release-workflow/fixtures/generate_r13_fixture.py
  version: 1.0
source_defaults:
  source_genome_build: GRCh38
  license: FinnGen public data
"""


def write_source(path: Path, variants: list[tuple]) -> None:
    """Write one FinnGen-shaped `.gz` source with a timestamp-free header."""
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                writer = csv.writer(text, delimiter="\t", lineterminator="\n")
                writer.writerow(FINNGEN_COLUMNS)
                for chrom, pos, ref, alt, beta, se, af, rsid in variants:
                    writer.writerow([chrom, pos, ref, alt, beta, se, af, rsid])


def canonical_alid(chrom: object, pos: object, ref: str, alt: str) -> str:
    """`chrom:pos:A1:A2`, A1 = min(ref, alt) -- the panel's ALID convention."""
    a1, a2 = sorted((ref.upper(), alt.upper()))
    return f"{chrom}:{pos}:{a1}:{a2}"


def a1_frequency(ref: str, alt: str, af_alt: float) -> float:
    """The A1-oriented frequency of a source row, from its alt frequency."""
    a1 = min(ref.upper(), alt.upper())
    return af_alt if alt.upper() == a1 else 1.0 - af_alt


def write_ancestry_reference() -> None:
    """The two-group mixture panel over exactly the fixture's variant sites.

    EUR's frequency is the first source row seen for each ALID (all three
    Analyses agree to within ~0.02); AFR's is its complement, so a study whose
    A1 frequencies match EUR fits as a EUR-dominant mixture.
    """
    eur: dict[str, float] = {}
    meta: dict[str, tuple[str, str, str, str]] = {}
    for entry in ENDPOINTS.values():
        for chrom, pos, ref, alt, _beta, _se, af_alt, _rsid in entry["variants"]:
            alid = canonical_alid(chrom, pos, ref, alt)
            eur.setdefault(alid, a1_frequency(ref, alt, af_alt))
            meta.setdefault(alid, (str(chrom), str(pos), min(ref.upper(), alt.upper()), max(ref.upper(), alt.upper())))

    ANCESTRY_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    with (ANCESTRY_REFERENCE_DIR / "ref_freqs.tsv.gz").open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
                writer.writerow([*ANCESTRY_REFERENCE_COLUMNS, *(group for group, _super in ANCESTRY_GROUPS)])
                for alid in sorted(eur):
                    chrom, pos, a1, a2 = meta[alid]
                    writer.writerow([
                        alid, chrom, pos, a1, a2, f"rs{pos}",
                        f"{eur[alid]:.6g}", f"{1.0 - eur[alid]:.6g}",
                    ])
    with (ANCESTRY_REFERENCE_DIR / "ancestry_groups.tsv").open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["group", "super_pop"])
        for group, super_pop in ANCESTRY_GROUPS:
            writer.writerow([group, super_pop])


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

    write_ancestry_reference()
    (FIXTURE_DIR / "release.yaml").write_text(RELEASE_YAML, encoding="utf-8")

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
