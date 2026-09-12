#!/usr/bin/env python3
"""Integration test for the dense one-pass AF/SE annotation stage."""
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
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def main() -> None:
    subprocess.run([sys.executable, "tests/ancestry-assignment/fixtures/generate_fixtures.py"], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory() as tmp_raw:
        tmp = Path(tmp_raw)
        release = tmp / "release"
        release.mkdir()
        vcf = tmp / "FIXT_DENSE.vcf"
        reference_rows: list[dict[str, str]] = []
        with gzip.open(REFERENCE / "ref_freqs.hg38.tsv.gz", "rt", newline="") as fh:
            reference_rows = list(csv.DictReader(fh, delimiter="\t"))
        with vcf.open("w", encoding="utf-8") as fh:
            fh.write("##fileformat=VCFv4.2\n")
            fh.write("##contig=<ID=1,length=1000000,assembly=GRCh38>\n")
            fh.write('##FORMAT=<ID=AF,Number=1,Type=Float,Description="Alternate allele frequency">\n')
            fh.write('##FORMAT=<ID=SE,Number=1,Type=Float,Description="Standard error">\n')
            fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tFIXT_DENSE\n")
            for row in reference_rows:
                canonical_af = float(row["EUR_fine"])
                alt_af = 1.0 - canonical_af
                se = 1.0 / math.sqrt(2 * 10000 * canonical_af * (1 - canonical_af))
                fh.write(
                    f"1\t{row['position']}\t{row['rsid']}\t{row['effect_allele']}\t{row['other_allele']}"
                    f"\t.\tPASS\t.\tAF:SE\t{alt_af:.12g}:{se:.12g}\n"
                )
        source = tmp / "FIXT_DENSE.vcf.gz"
        subprocess.run(["bcftools", "view", "-Oz", "-o", source, vcf], check=True)
        subprocess.run(["bcftools", "index", "-t", source], check=True)

        columns = [
            "analysis_id", "source_analysis_id", "source_label", "trait_ontology_label", "trait_ontology_id",
            "trait_ontology_mapping_method",
            "source_file", "source_genome_build", "source_ancestry_label", "assigned_ancestry",
            "ancestry_assignment_method", "original_effect_scale", "original_sd", "original_sd_method",
            "stored_effect_scale", "sample_size_kind", "sample_size_scope", "sample_size", "n_cases", "n_controls",
            "exclude_from_build",
        ]
        row = {
            "analysis_id": "FIXT_DENSE", "source_analysis_id": "FIXT_DENSE", "source_label": "Dense fixture",
            "trait_ontology_label": "", "trait_ontology_id": "", "trait_ontology_mapping_method": "unmapped",
            "source_file": str(source),
            "source_genome_build": "GRCh38", "source_ancestry_label": "European", "assigned_ancestry": "",
            "ancestry_assignment_method": "unassigned", "original_effect_scale": "native", "original_sd": "",
            "original_sd_method": "unavailable", "stored_effect_scale": "sd", "sample_size_kind": "total",
            "sample_size_scope": "analysis_level", "sample_size": "10000", "n_cases": "", "n_controls": "",
            "exclude_from_build": "",
        }
        with (release / "analyses.tsv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, delimiter="\t", fieldnames=columns, lineterminator="\n")
            writer.writeheader(); writer.writerow(row)
        (release / "build.yaml").write_text(f"""store_family_id: fixture
normalisation:
  target_reference_assembly: GRCh38
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
""", encoding="utf-8")
        (release / "validation.yaml").write_text("checks:\n  ancestry: not_run\n  effect_scale: not_run\n  sd_estimation: not_run\nreports: {{}}\nwarnings: []\n", encoding="utf-8")

        subprocess.run([
            sys.executable, "generators/lib/source-formats/opengwas-gwas-vcf-dense/annotate.py",
            f"--release-dir={release}",
        ], cwd=ROOT, check=True)
        analysis = read_rows(release / "analyses.tsv")[0]
        ancestry = read_rows(release / "sidecars/ancestry.tsv")[0]
        sd = read_rows(release / "sidecars/sd_estimation.tsv")[0]
        assert analysis["assigned_ancestry"] == "EUR"
        assert analysis["ancestry_assignment_method"] == "af_assigned"
        assert int(ancestry["af_overlap"]) == 40
        assert analysis["original_sd_method"] == "estimated_from_source_maf"
        assert abs(float(analysis["original_sd"]) - 1.0) < 1e-6
        assert sd["status"] == "passed" and sd["estimator_version"].startswith("opengwasdb:")
        validation = (release / "validation.yaml").read_text(encoding="utf-8")
        assert "ancestry: passed" in validation and "effect_scale: passed" in validation
        assert analysis["trait_ontology_mapping_method"] == "unmapped"
    print("ALL 9 CHECKS PASSED")


if __name__ == "__main__":
    main()
