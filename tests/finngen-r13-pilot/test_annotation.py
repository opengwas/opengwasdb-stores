#!/usr/bin/env python3
"""FinnGen fixture through the public one-pass annotation CLI."""
from __future__ import annotations

import csv
import gzip
import math
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "tests/ancestry-assignment/fixtures/reference"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_finngen(path: Path, reference_rows: list[dict[str, str]], sample_size: int) -> None:
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["#chrom", "pos", "ref", "alt", "beta", "sebeta", "af_alt"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in reference_rows:
            af = float(row["EUR_fine"])
            se = 1.0 / math.sqrt(2 * sample_size * af * (1 - af))
            writer.writerow({
                "#chrom": row["chromosome"],
                "pos": row["position"],
                "ref": row["other_allele"],
                "alt": row["effect_allele"],
                "beta": 0.2,
                "sebeta": f"{se:.12g}",
                "af_alt": row["EUR_fine"],
            })


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_raw:
        tmp = Path(tmp_raw)
        release = tmp / "release"
        release.mkdir()
        with gzip.open(REFERENCE / "ref_freqs.hg38.tsv.gz", "rt", newline="") as handle:
            reference_rows = list(csv.DictReader(handle, delimiter="\t"))
        binary_source = tmp / "finngen_R13_BINARY.gz"
        quant_source = tmp / "finngen_R13_QUANT.gz"
        write_finngen(binary_source, reference_rows, 10_000)
        write_finngen(quant_source, reference_rows, 10_000)

        columns = [
            "analysis_id", "source_analysis_id", "source_label", "analysis_label",
            "trait_ontology_label", "trait_ontology_id", "trait_ontology_mapping_method",
            "source_file", "source_reader_capability", "source_genome_build",
            "source_ancestry_label", "assigned_ancestry", "ancestry_assignment_method",
            "original_effect_scale", "original_sd", "original_sd_method",
            "stored_effect_scale", "sample_size_kind", "sample_size_scope", "sample_size",
            "n_cases", "n_controls", "exclude_from_build",
        ]
        common = {
            "trait_ontology_label": "",
            "trait_ontology_id": "",
            "trait_ontology_mapping_method": "unmapped",
            "source_reader_capability": "opengwasdb.finngen-r13",
            "source_genome_build": "GRCh38",
            "source_ancestry_label": "Finnish",
            "assigned_ancestry": "",
            "ancestry_assignment_method": "unassigned",
            "sample_size_scope": "analysis_level",
            "sample_size": "10000",
            "exclude_from_build": "",
        }
        rows = [
            {
                **common,
                "analysis_id": "finngen-r13-BINARY",
                "source_analysis_id": "BINARY",
                "source_label": "Binary fixture",
                "analysis_label": "Binary fixture",
                "source_file": str(binary_source),
                "original_effect_scale": "log_or",
                "original_sd": "",
                "original_sd_method": "binary_trait",
                "stored_effect_scale": "log_or",
                "sample_size_kind": "case_control",
                "n_cases": "1000",
                "n_controls": "9000",
            },
            {
                **common,
                "analysis_id": "finngen-r13-QUANT",
                "source_analysis_id": "QUANT",
                "source_label": "Quantitative fixture",
                "analysis_label": "Quantitative fixture",
                "source_file": str(quant_source),
                "original_effect_scale": "sd",
                "original_sd": "",
                "original_sd_method": "declared_standardised",
                "stored_effect_scale": "sd",
                "sample_size_kind": "total",
                "n_cases": "",
                "n_controls": "",
            },
        ]
        with (release / "analyses.tsv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        (release / "build.yaml").write_text(
            f"""store_family_id: finngen-r13-fixture
source:
  source_reader_capability: opengwasdb.finngen-r13
  source_genome_build: GRCh38
normalisation:
  target_reference_assembly: GRCh38
  liftover: none
reference_resources:
- resource_id: fixture-mixture
  kind: ancestry_mixture
  location: {REFERENCE / 'ref_freqs.hg38.tsv.gz'}
  fine_group_map: {REFERENCE / 'ancestry_groups.tsv'}
ancestry_assignment:
  enabled: yes
  reference_resource_id: fixture-mixture
  sampling_chromosome: 1
  max_reference_sites: 40
  maf_floor: 0.01
  gates:
    tau: 0.50
    delta: 0.20
    n_min: 10
    residual_max: 0.06
effect_scale_validation:
  enabled: yes
  min_overlap_variants: 10
  sd_tolerance: 0.15
""",
            encoding="utf-8",
        )
        (release / "validation.yaml").write_text(
            "checks:\n  ancestry: not_run\n  effect_scale: not_run\n  sd_estimation: not_run\n"
            "reports: {}\nwarnings: []\nerrors: []\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                "resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/annotate.py",
                f"--release-dir={release}",
                "--workers=2",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "one AF/SE extraction pass each" in result.stdout
        analyses = {row["analysis_id"]: row for row in read_rows(release / "analyses.tsv")}
        ancestry = {row["analysis_id"]: row for row in read_rows(release / "sidecars/ancestry.tsv")}
        sd = {row["analysis_id"]: row for row in read_rows(release / "sidecars/sd_estimation.tsv")}
        assert analyses["finngen-r13-BINARY"]["assigned_ancestry"] == "EUR"
        assert analyses["finngen-r13-QUANT"]["assigned_ancestry"] == "EUR"
        for analysis_id in ("finngen-r13-BINARY", "finngen-r13-QUANT"):
            sidecar_proportions = {
                column: value
                for column, value in ancestry[analysis_id].items()
                if column.startswith("ancestry_prop_")
            }
            assert sidecar_proportions
            for column, value in sidecar_proportions.items():
                assert column in analyses[analysis_id]
                assert math.isclose(
                    float(analyses[analysis_id][column]),
                    float(value),
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
        assert int(ancestry["finngen-r13-BINARY"]["af_overlap"]) == 40
        assert int(ancestry["finngen-r13-QUANT"]["af_overlap"]) == 40
        assert sd["finngen-r13-BINARY"]["status"] == "skipped"
        assert sd["finngen-r13-BINARY"]["skip_reason"] == "non_quantitative_effect_scale"
        assert analyses["finngen-r13-BINARY"]["original_sd"] == ""
        assert sd["finngen-r13-QUANT"]["status"] == "passed"
        assert abs(float(sd["finngen-r13-QUANT"]["implied_sd_median"]) - 1.0) < 1e-6
        validation = (release / "validation.yaml").read_text(encoding="utf-8")
        assert "ancestry: passed" in validation
        assert "effect_scale: passed_with_warnings" in validation
    print("ALL 13 CHECKS PASSED")


if __name__ == "__main__":
    main()
