#!/usr/bin/env python3
"""Regenerate the tiny catalogue-routed Hybrid fixture release (issue #104).

The catalogue-routed pre-build path is `assign-ancestry` -> `route-catalogue` ->
`build-hybrid-from-catalogue`. This fixture is the smallest release that
exercises all three through the real `opengwasdb` CLI and the real Snakemake DAG:

* it is **catalogue-routed** (`build.command: build-hybrid-from-catalogue`), so
  the workflow must resolve to a routed Analysis Catalogue rather than a builder
  manifest;
* its three Analyses are harmonised GWAS-SSF (the format the real
  `gwas-catalog-eur-hybrid` pilot uses), read through `opengwasdb.gwas-ssf`;
* two Analyses carry the ancestry reference's European frequencies and one the
  African frequencies, so ancestry assignment admits two as `EUR` and one as
  `AFR` -- and the build, filtering on `EUR`, keeps two and parks one. That is
  what makes the routing decision observable rather than implied;
* every Analysis carries 23 variants spanning all 22 autosomes, so the
  route-catalogue coverage gate (all autosomes, no single-chromosome
  concentration) is genuinely satisfied at fixture scale despite the tiny input.

The ancestry-mixture reference under `fixtures/ancestry-reference/` is reused
unchanged: its eight sites cover chromosomes 1-7, and the other fifteen fixture
variants (chromosomes 8-22) supply the coverage.

Run from the repository root:

    python3 tests/release-workflow/fixtures/generate_catalogue_fixture.py
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = FIXTURES_DIR / "catalogue-fixture"
SOURCE_DIR = FIXTURE_DIR / "source"
PANEL_DIR = FIXTURE_DIR / "panel"
ANCESTRY_REFERENCE = FIXTURES_DIR / "ancestry-reference" / "ref_freqs.tsv.gz"

#: Harmonised GWAS-SSF columns this fixture reproduces. `opengwasdb`'s GWAS-SSF
#: reader reads chromosome/base_pair_location/effect_allele/other_allele/beta/
#: standard_error/effect_allele_frequency (and rsid), so the fixture is a real
#: GWAS-SSF file, not a look-alike.
SSF_COLUMNS = (
    "chromosome",
    "base_pair_location",
    "effect_allele",
    "other_allele",
    "beta",
    "standard_error",
    "effect_allele_frequency",
    "p_value",
    "variant_id",
    "rsid",
    "ref_allele",
    "n",
)

#: Reference sites shared with `fixtures/ancestry-reference/`. Each is one
#: chromosome 1-7, so the remaining analyses' variant set covers all 22
#: autosomes once the chromosomes-8-22 filler below is added.
REFERENCE_SITES = ("1:1000000:A:G", "1:2000000:C:T", "2:3000000:A:G", "3:4000000:C:T",
                   "4:7000000:G:T", "5:5000000:A:T", "6:6000000:C:G", "7:8000000:A:C")

#: One extra variant on each autosome not already covered by REFERENCE_SITES.
FILLER_CHROMOSOMES = tuple(str(number) for number in range(8, 23))

#: `analysis_id` -> the fine ancestry group whose reference frequencies the
#: Analysis reports. Two European Analyses and one African: the build keeps the
#: European ones and parks the African one.
ANALYSES = {
    "catalogue-fixture-EUR_1": "EUR_fine",
    "catalogue-fixture-EUR_2": "EUR_fine",
    "catalogue-fixture-AFR_1": "AFR_fine",
}

#: The Dense Component's variant panel: three canonical ALIDs, one panel-only
#: (`1:1500000:C:T`), so the Dense fill and the Ragged Overflow are both real.
PANEL_ALIDS = ("1:1000000:A:G", "1:1500000:C:T", "1:2000000:C:T")

ANALYSES_COLUMNS = (
    "analysis_id",
    "source_analysis_id",
    "analysis_label",
    "source_file",
    "checksum",
    "checksum_algorithm",
    "source_reader_capability",
    "source_genome_build",
    "license",
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

BUILD_YAML = """\
store_family_id: catalogue-fixture
family_release_id: catalogue-fixture
store_layout: hybrid-observed
completion_state: observed-only
source:
  root: source
  analyses: analyses.tsv
  reader:
    capability: opengwasdb.gwas-ssf
normalisation:
  source_assembly: GRCh38
build:
  command: build-hybrid-from-catalogue
  arguments:
    store-id: catalogue-fixture
    release-id: catalogue-fixture
    stored-effect-scale: log_or
    original-sd-method: binary_trait
    ancestry: EUR
    n-workers: 1
artifacts:
  artifact_root: .release-work
  release_subdir: catalogue-fixture/releases/catalogue-fixture
reference_resources:
- resource_id: fixture-ancestry-mixture-hg38
  kind: ancestry_mixture
  location: tests/release-workflow/fixtures/ancestry-reference/ref_freqs.tsv.gz
  fine_group_map: tests/release-workflow/fixtures/ancestry-reference/ancestry_groups.tsv
- resource_id: fixture-hybrid-dense-panel
  kind: hybrid_dense_panel
  location: tests/release-workflow/fixtures/catalogue-fixture/panel/alids.txt
ancestry_assignment:
  enabled: yes
  reference_resource_id: fixture-ancestry-mixture-hg38
  maf_floor: 0.0
  gates:
    tau: 0.5
    delta: 0.2
    n_min: 2
    residual_max: 0.2
routing:
  min_variants: 5
  workers: 1
rho:
  enabled: false
reference_completion:
  enabled: false
validation:
  required: yes
"""

RELEASE_YAML = """\
metadata_schema_version: 1.0
store_family_id: catalogue-fixture
family_release_id: catalogue-fixture
status: candidate
source_collection_id: gwas-catalog-ssf
source_snapshot_id: catalogue-fixture
release_kind: one-off
association_coverage: full_gwas
description: >
  Tiny catalogue-routed Hybrid fixture release for the production Store Release
  workflow (issue #104): assign-ancestry -> route-catalogue ->
  build-hybrid-from-catalogue.
generator:
  name: tests/release-workflow/fixtures/generate_catalogue_fixture.py
  version: 1.0
source_defaults:
  source_genome_build: GRCh38
  license: fixture data
"""


def read_reference_frequencies() -> dict[str, dict[str, str]]:
    """The reference AP frequencies per site, keyed by canonical ALID."""
    sites: dict[str, dict[str, str]] = {}
    with gzip.open(ANCESTRY_REFERENCE, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            sites[row["alid"]] = row
    return sites


def write_source(path: Path, rows: list[list[str]]) -> None:
    """Write one GWAS-SSF `.gz` file with a timestamp-free header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as writer:
                csv_writer = csv.writer(writer, delimiter="\t", lineterminator="\n")
                csv_writer.writerow(SSF_COLUMNS)
                csv_writer.writerows(rows)


def source_rows(
    reference: dict[str, dict[str, str]], group: str
) -> list[list[str]]:
    """The 23 GWAS-SSF rows one Analysis carries, in fixed order."""
    rows: list[list[str]] = []
    for alid in REFERENCE_SITES:
        site = reference[alid]
        chromosome = site["chromosome"]
        position = site["position"]
        effect = site["effect_allele"]
        other = site["other_allele"]
        af = site[group]
        rows.append([
            chromosome, position, effect, other, "0.12", "0.05", af, "1e-8",
            site["rsid"], site["rsid"], other, "5000",
        ])
    for chromosome in FILLER_CHROMOSOMES:
        position = str(int(chromosome) * 1_000_000)
        rows.append([
            chromosome, position, "A", "G", "0.05", "0.05", "0.5", "1e-3",
            f"{chromosome}:{position}:A:G", f"rs{chromosome}000000", "G", "5000",
        ])
    return rows


def main() -> None:
    reference = read_reference_frequencies()
    sources: dict[str, Path] = {}
    for analysis_id, group in ANALYSES.items():
        file_name = f"{analysis_id}.h.tsv.gz"
        path = SOURCE_DIR / file_name
        write_source(path, source_rows(reference, group))
        sources[analysis_id] = path

    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    (PANEL_DIR / "alids.txt").write_text("".join(f"{alid}\n" for alid in PANEL_ALIDS), encoding="utf-8")

    fieldnames = list(ANALYSES_COLUMNS)
    with (FIXTURE_DIR / "analyses.tsv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for index, (analysis_id, _group) in enumerate(ANALYSES.items()):
            source = sources[analysis_id]
            writer.writerow({
                "analysis_id": analysis_id,
                "source_analysis_id": analysis_id,
                "analysis_label": f"Catalogue fixture analysis {index + 1}",
                "source_file": source.name,
                "checksum": hashlib.sha256(source.read_bytes()).hexdigest(),
                "checksum_algorithm": "sha256",
                "source_reader_capability": "opengwasdb.gwas-ssf",
                "source_genome_build": "GRCh38",
                "license": "fixture data",
                "source_ancestry_label": "European" if _group == "EUR_fine" else "African",
                "assigned_ancestry": "",
                "ancestry_assignment_method": "",
                "original_effect_scale": "log_or",
                "original_sd": "",
                "original_sd_method": "binary_trait",
                "stored_effect_scale": "log_or",
                "sample_size_kind": "case_control",
                "sample_size_scope": "analysis_level",
                "sample_size": "5000",
                "n_cases": "2500",
                "n_controls": "2500",
                "exclude_from_build": "",
            })

    (FIXTURE_DIR / "build.yaml").write_text(BUILD_YAML, encoding="utf-8")
    (FIXTURE_DIR / "release.yaml").write_text(RELEASE_YAML, encoding="utf-8")
    print(f"wrote {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
