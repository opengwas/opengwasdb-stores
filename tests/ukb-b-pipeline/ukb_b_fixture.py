#!/usr/bin/env python3
"""The ukb-b-shaped fixture the #289 pipeline suites share.

Three Analyses with the mirror's shape: a ``<id>.vcf.gz``, its ``.tbi`` index and
its provider JSON sidecar in one directory, the registry's declared selection
listing them, and the two recorded receipts the Acquisition Manifest writer
reads (checksums and integrity; a deep verification report with nothing to
report). The fixture then runs the two ukb-b writers -- the Source Metadata
Table adapter and the Acquisition Manifest writer -- so every suite starts from
connector files the real tools wrote, not from a hand-built table.

Nothing here reads the real mirror or the real receipts: the bodies are fixture
bytes and the receipts are written from them, so the suites are hermetic and
run anywhere. The real receipts' shape is the one
``/data/opengwasdb/work/epic232/219/ukb-b-restored-2026-10-11.{checksums,integrity}.json``
records, which is what the writer's docstring cites.

Not a suite: the file is named after neither ``test_*`` nor ``run_tests.py``, so
the runner does not collect it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from manifest_generation.providers.ukb_b.source_metadata_adapter import convert
from manifest_generation.providers.ukb_b.write_acquisition_manifest import (
    Evidence,
    write_from_evidence,
)

#: The three fixture Analyses: (analysis_id, provider JSON, body seed).
#: Two case-control traits and one quantitative, so both method tiers are used.
ANALYSES: tuple[tuple[str, dict[str, object], bytes], ...] = (
    (
        "ukb-b-10001",
        {
            "id": "UKB-b-10001",
            "trait": "Operative procedures - secondary OPCS: Z84.6 Knee joint",
            "author": "Ben Elsworth",
            "population": "European",
            "sample_size": 463010,
            "ncase": "6661",
            "ncontrol": "456349",
            "units": "SD",
            "build": "HG19/GRCh37",
            "batch": "UKB-b",
            "consortium": "MRC-IEU",
        },
        b"fixture-vcf-10001\n",
    ),
    (
        "ukb-b-10054",
        {
            "id": "UKB-b-10054",
            "trait": "Cheese consumers",
            "author": "Ben Elsworth",
            "population": "European",
            "sample_size": 64949,
            "ncase": "29408",
            "ncontrol": "35541",
            "units": "SD",
            "build": "HG19/GRCh37",
            "batch": "UKB-b",
            "consortium": "MRC-IEU",
        },
        b"fixture-vcf-10054\n" * 3,
    ),
    (
        "ukb-b-10787",
        {
            "id": "UKB-b-10787",
            "trait": "Standing height",
            "author": "Ben Elsworth",
            "population": "European",
            "sample_size": 461950,
            "ncase": "NA",
            "ncontrol": "NA",
            "units": "SD",
            "build": "HG19/GRCh37",
            "batch": "UKB-b",
            "consortium": "MRC-IEU",
        },
        b"fixture-vcf-10787\n" * 2,
    ),
)

DECLARED_COLUMNS: tuple[str, ...] = ("analysis_id", "source_analysis_id", "source_label")
TRAIT_TABLE_RESOURCE_ID = "canonical-trait-mapping-efo"
TRAIT_TABLE_TEXT = (
    "trait_label\ttrait_ontology_id\ttrait_ontology_label\n"
    "standing height\tEFO:0004339\tbody height\n"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_tsv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(column, "") for column in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class UkbFixture:
    """One ukb-b-shaped Source Collection, its connector files and a release config."""

    root: Path
    source_dir: Path
    declared_selection: Path
    checksums: Path
    integrity: Path
    verification: Path
    source_metadata_table: Path
    acquisition_manifest: Path
    inventory_dir: Path
    config_path: Path
    outcomes_path: Path
    reference: Path
    trait_table_dir: Path
    registry_root: Path
    work_root: Path
    resolution_root: Path
    snapshot_id: str


def build_fixture(
    root: Path,
    *,
    snapshot_id: str = "ukb-b-fixture-2026-10-22",
    label: str = "ukb-b-fixture",
    selection: dict[str, object] | None = None,
) -> UkbFixture:
    """Write the mirror, the declared selection, the receipts and a release config."""
    root = Path(root)
    source_dir = root / "source"
    source_dir.mkdir(parents=True)
    declared_rows = []
    checksums: dict[str, dict[str, object]] = {}
    integrity: dict[str, dict[str, object]] = {}
    for analysis_id, document, body in ANALYSES:
        data = source_dir / f"{analysis_id}.vcf.gz"
        data.write_bytes(body)
        data.with_name(data.name + ".tbi").write_bytes(f"index-{analysis_id}".encode())
        (source_dir / f"{analysis_id}.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
        declared_rows.append(
            {
                "analysis_id": analysis_id,
                "source_analysis_id": analysis_id,
                "source_label": str(document["trait"]),
            }
        )
        checksums[analysis_id] = {
            "data_file": str(data),
            "data_bytes": len(body),
            "mtime_ns": data.stat().st_mtime_ns,
            "sha256": sha256_bytes(body),
        }
        integrity[analysis_id] = {
            "status": "ok",
            "detail": "contigs=22",
            "data_bytes": len(body),
            "mtime_ns": data.stat().st_mtime_ns,
        }
    declared_selection = root / "declared-selection.tsv"
    write_tsv(declared_selection, DECLARED_COLUMNS, declared_rows)
    checksums_path = root / "checksums.json"
    checksums_path.write_text(json.dumps({"schema_version": 1, "entries": checksums}), encoding="utf-8")
    integrity_path = root / "integrity.json"
    integrity_path.write_text(json.dumps({"schema_version": 1, "entries": integrity}), encoding="utf-8")
    verification_path = root / "verify-deep.json"
    verification_path.write_text(
        json.dumps(
            {
                "checksum_mismatch": [],
                "deep": True,
                "index_mismatch": [],
                "missing": [],
                "mtime_changed": [],
                "ok": True,
                "rows": len(ANALYSES),
                "size_mismatch": [],
            }
        ),
        encoding="utf-8",
    )

    # The two writers, exactly as the release config's connectors are built.
    source_metadata_table = root / "source-metadata-table.tsv"
    convert(declared_selection, source_dir, source_metadata_table)
    acquisition_manifest = root / "acquisition-manifest.tsv"
    write_from_evidence(
        declared_selection,
        source_dir,
        Evidence(checksums_path, integrity_path, verification_path),
        acquisition_manifest,
    )

    # The reference resources a release config must name, at fixture scale.
    reference = root / "reference"
    reference.mkdir()
    (reference / "ref_freqs.tsv.gz").write_bytes(b"fixture reference\n")
    (reference / "ancestry_groups.tsv").write_text(
        "fine_group\tsuper_population\nEUR_A\tEUR\n", encoding="utf-8"
    )
    trait_table_dir = root / "resources" / "reference-resources" / TRAIT_TABLE_RESOURCE_ID
    trait_table_dir.mkdir(parents=True)
    (trait_table_dir / "resource.yaml").write_text(
        f"resource_id: {TRAIT_TABLE_RESOURCE_ID}\nkind: trait_ontology_mapping\nversion: 1\n"
        f"location: resources/reference-resources/{TRAIT_TABLE_RESOURCE_ID}/mapping.tsv\n",
        encoding="utf-8",
    )
    (trait_table_dir / "mapping.tsv").write_text(TRAIT_TABLE_TEXT, encoding="utf-8")

    inventory_dir = root / "inventory"
    inventory_dir.mkdir()
    work_root = root / "work"
    registry_root = root / "stores"
    registry_root.mkdir()
    resolution_root = root / "resolution"
    config = {
        "release_config_version": 1,
        "label": label,
        "access_posture": "public",
        "description": "three ukb-b-shaped fixture Analyses",
        "source_collection": {
            "source_collection_id": "opengwas-gwas-vcf",
            "connectors": {
                "source_metadata_table": str(source_metadata_table),
                "acquisition_manifest": str(acquisition_manifest),
            },
            "inventory": {"snapshot_id": snapshot_id, "directory": str(inventory_dir)},
        },
        "selection": selection or {"all": True, "expect_rows": len(ANALYSES)},
        "build": {
            "layout": "dense",
            "completion_state": "observed_only",
            "command": "build-dense-vcf",
            "options": {
                "source-reader-capability": "opengwasdb.gwas-vcf",
                "source-assembly": "hg19",
            },
            "post": {"top_hits": True, "overview": True},
        },
        "defaults": {
            "source_genome_build": "GRCh37",
            "license": "UK Biobank terms of access",
            "consortium": "MRC-IEU",
            "sample_size_scope": "analysis_level",
            "analysis_group_id": "ukb-b",
            "by_study_design": {
                "quantitative": {
                    "stored_effect_scale": "sd",
                    "original_effect_scale": "sd",
                    "original_sd_method": "declared_standardised",
                    "sample_size_kind": "total",
                },
                "case-control": {
                    "stored_effect_scale": "sd",
                    "original_effect_scale": "sd",
                    "original_sd_method": "declared_standardised",
                    "sample_size_kind": "case_control",
                },
            },
        },
        "release_policy": {"selection_superpopulation": "EUR"},
        "reference_resources": [
            {
                "resource_id": "ukb-ancestry-mixture-hg38",
                "kind": "ancestry_mixture",
                "location": str(reference / "ref_freqs.tsv.gz"),
                "fine_group_map": str(reference / "ancestry_groups.tsv"),
            },
            {
                "resource_id": TRAIT_TABLE_RESOURCE_ID,
                "kind": "trait_ontology_mapping",
                "location": str(trait_table_dir),
            },
        ],
        "ancestry_assignment": {
            "reference_resource_id": "ukb-ancestry-mixture-hg38",
            "maf_floor": 0.01,
            "extraction_panel": None,
            "gates": {"tau": 0.50, "delta": 0.20, "n_min": 5000, "residual_max": 0.06},
        },
        "effect_scale_validation": {"reference_resources": []},
        "trait_ontology_mapping": {"reference_resource_id": TRAIT_TABLE_RESOURCE_ID},
        "runtime": {"cores": 1, "min_free_gb": 0},
        "output": {"work_root": str(work_root)},
    }
    config_path = root / "release-fixture.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    outcomes_path = root / "outcomes.json"
    # Every fixture Analysis's method tier declares its scale, so the resolver
    # reports nothing to estimate and the phenotype_sd check is not applicable.
    outcomes_path.write_text(
        json.dumps(
            {
                analysis_id: {
                    "status": "success",
                    "assigned_ancestry": "EUR",
                    "gate_reason": "ok",
                    "eaf_orientation": "passed",
                    "sd_status": "skipped",
                    "sd_reason": "scale_already_declared",
                }
                for analysis_id, _document, _body in ANALYSES
            }
        ),
        encoding="utf-8",
    )
    return UkbFixture(
        root=root,
        source_dir=source_dir,
        declared_selection=declared_selection,
        checksums=checksums_path,
        integrity=integrity_path,
        verification=verification_path,
        source_metadata_table=source_metadata_table,
        acquisition_manifest=acquisition_manifest,
        inventory_dir=inventory_dir,
        config_path=config_path,
        outcomes_path=outcomes_path,
        reference=reference,
        trait_table_dir=trait_table_dir,
        registry_root=registry_root,
        work_root=work_root,
        resolution_root=resolution_root,
        snapshot_id=snapshot_id,
    )
