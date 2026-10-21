#!/usr/bin/env python3
"""Two MVP-shaped Analyses through the shared pipeline, into a temporary registry (#290).

This is the small end-to-end seam: the dbGaP MVP adapter writes the Source
Metadata Table, the acquisition writes a shared Acquisition Manifest, and
``prepare``, ``resolve`` (the hermetic fake resolver), ``render`` and ``emit``
produce a candidate bundle. It is kept under five seconds and reads no
association body.

Run from the repository root:
    python3 tests/dbgap-mvp/test_end_to_end.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.connector_tsv import write_connector
from manifest_generation.acquisition_manifest import ACQUISITION_MANIFEST
from manifest_generation.providers.dbgap.mvp.source_metadata_adapter import (
    convert_mapping,
)

FAKE_RESOLVER = REPO_ROOT / "tests/phase-b-candidate/fixtures/fake_resolver.py"
STORE_ID = "OGS-99000"
INNER = "MVP_R4.1000G_AGR.GIA.Fixture"
MAPPING_COLUMNS = (
    "analysis_id", "member", "tar", "ancestry", "study_design", "dbgap_trait_type",
    "sample_size", "n_cases", "n_controls", "dbgap_trait_code", "analyzed_variable",
    "candidate_trait", "candidate_store_key", "match_basis", "trait_check",
)
ANALYSES = (
    # analysis_id, trait code, analyzed variable, design, N, cases, controls.
    ("GCST90001001", "A1C_Min_INT", "hemoglobin A1c (minimum)", "quantitative", 338640, 0, 0),
    ("GCST90001002", "Phe_394", "Rheumatic disease of the heart valves", "case-control", 438595, 5583, 433012),
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(root: Path) -> Path:
    extracted = root / "extracted" / INNER
    extracted.mkdir(parents=True)
    mapping_rows: list[dict[str, str]] = []
    manifest_rows: list[dict[str, str]] = []
    for analysis_id, trait_code, variable, design, size, cases, controls in ANALYSES:
        stem = f"MVP_R4.1000G_AGR.{trait_code}.EUR.GIA"
        member = f"{INNER}/{stem}.dbGaP.txt.gz"
        sample = f"Total Sample Size={size}"
        if design == "case-control":
            sample += f"; Cases={cases}; Controls={controls}"
        (extracted / f"{stem}.dbGaP.metadata.txt").write_text(
            "Info\tDescription\n"
            f"Analyzed variable\t{variable}\n"
            f"Phenotypic trait type\t{'Binary Trait' if design == 'case-control' else 'Quantitative Trait'}\n"
            f"Sample size\t{sample}\n",
            encoding="utf-8",
        )
        mapping_rows.append({
            "analysis_id": analysis_id, "member": member, "tar": "phs002453.fixture.tar",
            "ancestry": "EUR", "study_design": design,
            "dbgap_trait_type": "binary" if design == "case-control" else "quantitative",
            "sample_size": str(size), "n_cases": str(cases), "n_controls": str(controls),
            "dbgap_trait_code": trait_code, "analyzed_variable": variable,
            "candidate_trait": variable, "candidate_store_key": "dense__pmid-39024449__European",
            "match_basis": "unique-key", "trait_check": "exact",
        })
        data = root / "mirror" / f"{analysis_id}.tsv.gz"
        data.parent.mkdir(parents=True, exist_ok=True)
        data.write_bytes(f"fixture body for {analysis_id}\n".encode())
        sidecar = data.with_name(data.name + "-meta.yaml")
        sidecar.write_text("genome_assembly: GRCh38\n", encoding="utf-8")
        manifest_rows.append({
            "source_analysis_id": analysis_id,
            "status": "ok",
            "location": str(data),
            "bytes": str(data.stat().st_size),
            "sha256": _sha256(data.read_bytes()),
            "sidecar_location": str(sidecar),
            "sidecar_bytes": str(sidecar.stat().st_size),
        })
    mapping = root / "mapping.tsv"
    with mapping.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MAPPING_COLUMNS), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(mapping_rows)
    metadata_table = root / "source-metadata.tsv"
    convert_mapping(mapping, root / "extracted", metadata_table, repo_root=REPO_ROOT)
    acquisition_manifest = root / "acquisition-manifest.tsv"
    write_connector(
        acquisition_manifest, ACQUISITION_MANIFEST, ACQUISITION_MANIFEST.columns, manifest_rows
    )

    reference = root / "reference"
    reference.mkdir()
    (reference / "ref_freqs.tsv.gz").write_bytes(b"fixture reference\n")
    (reference / "ancestry_groups.tsv").write_text(
        "fine_group\tsuper_population\nEUR_A\tEUR\n", encoding="utf-8"
    )
    trait_table = root / "trait-table"
    trait_table.mkdir()
    (trait_table / "resource.yaml").write_text(
        "resource_id: fixture-trait-table\nkind: trait_ontology_mapping\nversion: 1\n"
        "location: trait-table/mapping.tsv\nlocation_kind: tracked_file\n",
        encoding="utf-8",
    )
    (trait_table / "mapping.tsv").write_text(
        "trait_label\ttrait_ontology_id\ttrait_ontology_label\n", encoding="utf-8"
    )

    config = {
        "release_config_version": 1,
        "label": "fixture-mvp",
        "access_posture": "public",
        "description": "Two dbGaP MVP-shaped fixture Analyses.",
        "source_collection": {
            "source_collection_id": "dbgap-phs002453",
            "connectors": {
                "source_metadata_table": str(metadata_table),
                "acquisition_manifest": str(acquisition_manifest),
            },
            "inventory": {"snapshot_id": "fixture-mvp-snapshot", "directory": str(root / "inventory")},
        },
        "selection": {"all": True, "expect_rows": 2},
        "build": {
            "layout": "dense",
            "completion_state": "observed_only",
            "command": "build-dense-vcf",
            "options": {"source-reader-capability": "opengwasdb.gwas-ssf", "source-assembly": "GRCh38"},
            "post": {"top_hits": True, "overview": True},
        },
        "defaults": {
            "source_genome_build": "GRCh38",
            "license": "dbGaP open access",
            "by_study_design": {
                "quantitative": {
                    "stored_effect_scale": "sd", "original_effect_scale": "sd",
                    "original_sd_method": "declared_standardised", "sample_size_kind": "total",
                },
                "case-control": {
                    "stored_effect_scale": "log_or", "original_effect_scale": "log_or",
                    "original_sd_method": "binary_trait", "sample_size_kind": "case_control",
                },
            },
        },
        "release_policy": {"selection_superpopulation": "EUR"},
        # The tracked panel declaration gives the resolver its genome_build
        # (opengwasdb#277); a fixture panel stands in for its file.
        "reference_resources": [
            {
                "resource_id": "ukb-ancestry-mixture-hg38", "kind": "ancestry_mixture",
                "location": str(reference / "ref_freqs.tsv.gz"),
                "fine_group_map": str(reference / "ancestry_groups.tsv"), "version": "fixture",
            },
            {
                "resource_id": "fixture-trait-table", "kind": "trait_ontology_mapping",
                "location": str(trait_table), "version": "1",
            },
        ],
        "ancestry_assignment": {
            "reference_resource_id": "ukb-ancestry-mixture-hg38",
            "maf_floor": 0.01,
            "extraction_panel": None,
            "gates": {"tau": 0.50, "delta": 0.20, "n_min": 5000, "residual_max": 0.06},
        },
        "trait_ontology_mapping": {"reference_resource_id": "fixture-trait-table"},
        "runtime": {"cores": 1, "min_free_gb": 0},
        "output": {"work_root": str(root / "work")},
    }
    config_path = root / "release.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (root / "outcomes.json").write_text(json.dumps({}), encoding="utf-8")
    return config_path


class EndToEndTests(unittest.TestCase):
    def test_two_mvp_analyses_reach_a_candidate_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dbgap-e2e-") as tmp:
            root = Path(tmp)
            config = build(root)
            registry = root / "stores"
            env = {
                **os.environ,
                "PATH": str(Path(sys.executable).resolve().parent) + os.pathsep + os.environ.get("PATH", ""),
                "FAKE_RESOLVER_OUTCOMES": str(root / "outcomes.json"),
            }
            result = subprocess.run(
                [
                    sys.executable, "-m", "manifest_generation.generate_candidate", STORE_ID,
                    "--config", str(config), "--cores", "1",
                    "--resolver", str(FAKE_RESOLVER),
                    "--repo-root", str(REPO_ROOT),
                    "--registry-root", str(registry),
                    "--work-root", str(root / "work"),
                    "--resolution-root", str(root / "resolution"),
                    "--stage", "all",
                ],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            bundle = registry / STORE_ID
            self.assertTrue((bundle / "release.yaml").is_file())
            with (bundle / "analyses.tsv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual({row["analysis_id"] for row in rows}, {a[0] for a in ANALYSES})
            self.assertEqual({row["exclude_from_build"] for row in rows}, {""})
            scales = {row["analysis_id"]: row["stored_effect_scale"] for row in rows}
            self.assertEqual(scales, {"GCST90001001": "sd", "GCST90001002": "log_or"})
            self.assertTrue((bundle / "sidecars/quality_checks.tsv").is_file())
            release = yaml.safe_load((bundle / "release.yaml").read_text(encoding="utf-8"))
            self.assertEqual(release["status"], "candidate")


if __name__ == "__main__":
    unittest.main()
