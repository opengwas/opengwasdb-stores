#!/usr/bin/env python3
"""Hermetic tests for the Phase B candidate workflow (issue #153).

These are fixture-scale and network-free: a temporary mirror, candidate
metadata table, frozen Source Inventory and config are built in a temp
directory, and a fake `opengwasdb resolve-analyses` (``fixtures/fake_resolver.py``)
supplies the resolver contract -- atomic per-Analysis records, a deterministic
``index.json``, fingerprint inputs and ``--resume`` semantics -- with outcomes
the test chooses. The real resolver's statistics, reference loading and worker
pool are exercised upstream, not here.

Covered contracts:

1. mixed study designs and every controlled exclusion outcome (#152 policy);
2. duplicate-content accessions surfaced, not collapsed;
3. malformed/controlled-failure sources isolated to the affected Analysis;
4. resolver accounting: missing, stale, duplicate and extra records all fail
   finalisation before any bundle is replaced;
5. resume reuses unchanged successful records and reproduces the same bytes;
6. an interrupted resolver leaves a prior candidate untouched and no partial one;
7. 1 worker and many workers produce byte-identical bundle tables/sidecars;
8. the emitted analyses.tsv passes the pinned OpenGWASDB schema and the whole
   bundle passes bundle.check();
9. the operator entry point binds the frozen inventory, the #152 policy and the
   executed command log into release.yaml, and never writes anything but a
   candidate;
10. Resolution Records are kept once per Source Collection and reused by every
    Selection (#284): a Trial Store Release and a full release resolve each
    shared Analysis once; only a changed file, reference or setting
    re-resolves; each receipt names exactly its records and verification
    catches a missing, stale or edited one; a second run over a locked
    collection area is refused.

Run from the repository root:
    python3 tests/phase-b-candidate/test_phase_b_candidate.py
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

import manifest_generation.candidate_workflow as candidate_workflow  # noqa: E402
from ogstores import bundle as bundle_module  # noqa: E402
from ogstores.plan import plan  # noqa: E402
from manifest_generation.candidate_workflow import (  # noqa: E402
    EXCLUSION_REASONS,
    RECEIPT_FILENAME,
    RESOLVER_MANIFEST_COLUMNS,
    USABLE_MAF_STATES,
    CandidateError,
    CandidateMetadata,
    ResolverRow,
    account_records,
    analysis_maf_threshold,
    apply_release_policy,
    build_candidate_tables,
    compute_resolution_contract,
    derive_resolver_manifest,
    info_score_emission,
    load_candidate_configuration,
    maf_threshold_emission,
    parse_maf_filter_exempt_technologies,
    parse_maf_threshold,
    parse_store_composition,
    read_candidate_metadata,
    read_genotyping_technologies,
    read_effect_placeholder_exclusions,
    read_maf_filter_exempt_analyses,
    read_resolution_receipt,
    render_release_yaml,
    render_validation_yaml,
    render_resolver_manifest,
    resolver_argv,
    validate_candidate_analyses,
    verify_records,
)
from manifest_generation import generate_candidate  # noqa: E402
from manifest_generation.resolution_area import (  # noqa: E402
    DEFAULT_RESOLUTION_ROOT,
    RESOLUTION_ROOT_ENV_VAR,
    KeptRecord,
    ResolutionArea,
    ResolutionAreaError,
    ResolutionAreaLocked,
    resolution_root,
)
from manifest_generation.source_inventory import (  # noqa: E402
    ACQUISITION_MANIFEST_COLUMNS,
    AcquisitionPass,
    PreflightConfigError,
    SourceInventoryRow,
    build_snapshot,
    read_candidate_selection,
    read_inventory,
    write_snapshot,
)

CLI = ("-m", "manifest_generation.generate_candidate")
FAKE_RESOLVER = Path(__file__).resolve().parent / "fixtures/fake_resolver.py"
SNAKEMAKE = shutil.which("snakemake")
STORE_KEY = "hybrid__European"
SNAPSHOT_ID = "fixture-snapshot"
STORE_ID = "OGS-99001"

# (analysis_id, study_design, sample_size, readiness, content, n_cases, n_controls)
READY_ANALYSES = [
    ("GCST90000001", "quantitative", "5000", "ok", "A", "", ""),
    ("GCST90000002", "quantitative", "6000", "ok", "A", "", ""),
    ("GCST90000003", "quantitative", "7000", "ok", "B", "", ""),
    ("GCST90000004", "case-control", "8000", "ok", "C", "4000", "4000"),
    ("GCST90000005", "case-control", "9000", "ok", "D", "4500", "4500"),
    ("GCST90000006", "quantitative", "3000", "ok", "E", "", ""),
    ("GCST90000007", "quantitative", "4000", "ok", "F", "", ""),
    ("GCST90000008", "quantitative", "2000", "ok", "G", "", ""),
    ("GCST90000009", "case-control", "1000", "ok", "H", "", ""),
]
NON_READY = ("GCST90000010", "quantitative", "1000", "header_rejected", "", "", "")

# outcome per analysis_id, fed to the fake resolver
OUTCOMES = {
    # included quantitative, but high dispersion -> warning
    "GCST90000001": {"sd": 1.2, "sd_dispersion": 0.6},
    "GCST90000002": {"sd": 0.8, "sd_dispersion": 0.05},
    "GCST90000003": {"sd_status": "unavailable", "sd_reason": "no_qualifying_evidence"},
    "GCST90000004": {},
    "GCST90000005": {"assigned_ancestry": "EAS"},
    "GCST90000006": {"assigned_ancestry": None, "gate_reason": "overlap"},
    "GCST90000007": {
        "assigned_ancestry": None,
        "gate_reason": "eaf_orientation",
        "eaf_orientation": "failed",
        "eaf_orientation_r": -0.98,
    },
    "GCST90000008": {"status": "controlled_failure", "error": "simulated parse failure"},
    "GCST90000009": {},  # counts blank in the candidate table -> excluded
}

EXPECTED_INCLUDED = {"GCST90000001", "GCST90000002", "GCST90000004"}
EXPECTED_EXCLUDED = {
    "GCST90000003": "sd_no_qualifying_evidence",
    "GCST90000005": "ancestry_not_eur",
    "GCST90000006": "ancestry_unassigned",
    "GCST90000007": "orientation_failure",
    "GCST90000008": "resolution_failed",
    "GCST90000009": "missing_case_control_counts",
}

META_TEMPLATE = "gwas_id: {analysis_id}\ngenome_assembly: GRCh38\nis_harmonised: true\ncoordinate_system: 1-based\n"


@dataclass
class Fixture:
    root: Path
    config_path: Path
    inventory_path: Path
    candidates_path: Path
    outcomes_path: Path
    registry_root: Path
    work_root: Path
    resolution_root: Path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FixtureBuilder:
    """Build one self-consistent fixture tree in a temporary directory."""

    def __init__(self, root: Path, bodies=None) -> None:
        #: Optional ``analysis_id -> bytes`` for the mirror's source files; the
        #: default is a placeholder body only the fake resolver can read.
        self.bodies = bodies
        self.root = root
        self.mirror = root / "mirror"
        self.mirror.mkdir(parents=True, exist_ok=True)
        self.reference = root / "reference"
        self.reference.mkdir(parents=True, exist_ok=True)
        self.acq = root / "acq"
        self.acq.mkdir(parents=True, exist_ok=True)
        self.inventory_dir = root / "inventory"
        self.inventory_dir.mkdir(parents=True, exist_ok=True)

    def build(self) -> Fixture:
        (self.reference / "ref_freqs.tsv.gz").write_bytes(b"fixture reference\n")
        (self.reference / "ancestry_groups.tsv").write_text(
            "fine_group\tsuper_population\nEUR_A\tEUR\nEAS_A\tEAS\n", encoding="utf-8"
        )

        all_rows = READY_ANALYSES + [NON_READY]
        manifest_rows: list[dict[str, str]] = []
        for analysis_id, design, sample_size, readiness, content, _n_cases, _n_controls in all_rows:
            row = {name: "" for name in ACQUISITION_MANIFEST_COLUMNS}
            row["analysis_id"] = analysis_id
            row["publication_pmid"] = "12345678"
            row["trait"] = f"Trait {analysis_id}"
            row["study_design"] = design
            row["sample_size"] = sample_size
            row["status"] = readiness
            if readiness in {"ok", "already_present"}:
                body = (
                    self.bodies(analysis_id)
                    if self.bodies is not None
                    else (f"source-body-{content}\n" * (1 + ord(content[0]) % 3)).encode()
                )
                data_path = self.mirror / f"{analysis_id}.h.tsv.gz"
                data_path.write_bytes(body)
                meta_path = self.mirror / f"{analysis_id}.h.tsv.gz-meta.yaml"
                meta_path.write_text(
                    META_TEMPLATE.format(analysis_id=analysis_id), encoding="utf-8"
                )
                row["data_url"] = f"https://example.invalid/{analysis_id}.h.tsv.gz"
                row["yaml_url"] = f"https://example.invalid/{analysis_id}.h.tsv.gz-meta.yaml"
                row["data_file"] = str(data_path)
                row["yaml_file"] = str(meta_path)
                row["data_bytes"] = str(len(body))
                row["yaml_bytes"] = str(meta_path.stat().st_size)
                row["sha256"] = _sha256_bytes(body)
            manifest_rows.append(row)

        base_manifest = self.acq / "base.tsv"
        retry_manifest = self.acq / "retry.tsv"
        for path in (base_manifest, retry_manifest):
            _write_tsv(path, ACQUISITION_MANIFEST_COLUMNS, manifest_rows)

        candidates_path = self.root / "candidates.tsv"
        candidate_rows = []
        for analysis_id, design, sample_size, _readiness, _content, n_cases, n_controls in all_rows:
            candidate_rows.append(
                {
                    "STUDY.ACCESSION": analysis_id,
                    "store_key": STORE_KEY,
                    "study_design": design,
                    "n_cases": n_cases,
                    "n_controls": n_controls,
                    "sample_size": sample_size,
                    "DISEASE.TRAIT": f"Trait {analysis_id}",
                    "MAPPED_TRAIT": f"mapped {analysis_id}",
                    "MAPPED_TRAIT_URI": "http://purl.obolibrary.org/obo/MONDO_0005148",
                    "PUBMED.ID": "12345678",
                    "FIRST.AUTHOR": "Author A",
                }
            )
        _write_tsv(
            candidates_path,
            [
                "STUDY.ACCESSION",
                "store_key",
                "study_design",
                "n_cases",
                "n_controls",
                "sample_size",
                "DISEASE.TRAIT",
                "MAPPED_TRAIT",
                "MAPPED_TRAIT_URI",
                "PUBMED.ID",
                "FIRST.AUTHOR",
            ],
            candidate_rows,
        )

        selection = read_candidate_selection(candidates_path, STORE_KEY)
        snapshot = build_snapshot(
            snapshot_id=SNAPSHOT_ID,
            source_collection_id="gwas-catalog-ssf",
            store_key=STORE_KEY,
            ancestry_group="European",
            manifests=[
                AcquisitionPass(role="base", path=base_manifest),
                AcquisitionPass(role="retry_transient", path=retry_manifest),
            ],
            candidates=selection,
            frozen_at="2026-09-21T00:00:00Z",
        )
        inventory_path = self.inventory_dir / f"{SNAPSHOT_ID}.tsv"
        provenance_path = self.inventory_dir / f"{SNAPSHOT_ID}.meta.yaml"
        write_snapshot(snapshot, inventory_path, provenance_path)

        work_root = self.root / "work"
        registry_root = self.root / "stores"
        registry_root.mkdir(parents=True, exist_ok=True)

        config = {
            "label": "fixture-candidate",
            "access_posture": "public",
            "description": "fixture candidate",
            "notes": "fixture notes",
            "source": {
                "source_collection_id": "gwas-catalog-ssf",
                "store_key": STORE_KEY,
                "ancestry_group": "European",
                "candidates": str(candidates_path),
                "inventory": {
                    "snapshot_id": SNAPSHOT_ID,
                    "path": str(inventory_path),
                    "provenance_path": str(provenance_path),
                    "freeze_inputs": [
                        {"role": "base", "path": str(base_manifest)},
                        {"role": "retry_transient", "path": str(retry_manifest)},
                    ],
                },
            },
            "defaults": {
                "source_genome_build": "GRCh38",
                "license": "fixture license",
                "sample_size_scope": "analysis_level",
                "ancestry_assignment_method": "source_trusted_no_af",
                "by_study_design": {
                    "quantitative": {
                        "stored_effect_scale": "sd",
                        "original_effect_scale": "sd",
                        "original_sd_method": "estimated_from_source_maf",
                        "sample_size_kind": "total",
                    },
                    "case-control": {
                        "stored_effect_scale": "log_or",
                        "original_effect_scale": "log_or",
                        "original_sd_method": "binary_trait",
                        "sample_size_kind": "case_control",
                    },
                },
            },
            "reference_resources": [
                {
                    "resource_id": "fixture-ancestry-mixture",
                    "kind": "ancestry_mixture",
                    "ancestry": "multi",
                    "super_populations": ["AFR", "AMR", "EAS", "EUR", "MID", "NAF", "SAS"],
                    "genome_build": "GRCh38",
                    "variant_id_convention": "chr:pos:A1:A2",
                    "location": str(self.reference / "ref_freqs.tsv.gz"),
                    "location_kind": "external_file",
                    "version": "fixture-v1",
                    "fine_group_map": str(self.reference / "ancestry_groups.tsv"),
                }
            ],
            "ancestry_assignment": {
                "enabled": True,
                "reference_resource_id": "fixture-ancestry-mixture",
                "maf_floor": 0.01,
                "extraction_panel": None,
                "gates": {"tau": 0.50, "delta": 0.20, "n_min": 5000, "residual_max": 0.06},
            },
            "effect_scale_validation": {
                "enabled": True,
                "maf_min": 0.01,
                "maf_max": 0.5,
                "min_overlap_variants": 20,
                "sd_tolerance": 0.15,
                "warning_multiplier": 2.0,
                "dispersion_max": 0.5,
                "reference_resources": [],
            },
            "runtime": {"cores": 4, "min_free_gb": 0},
            "output": {"work_root": str(work_root)},
            "build": {
                "layout": "hybrid",
                "completion_state": "observed_only",
                "command": "build-hybrid",
                "options": {
                    "source-reader-capability": "opengwasdb.gwas-ssf",
                    "source-assembly": "hg38",
                },
                "post": {"top_hits": False, "overview": True},
            },
        }
        config_path = self.root / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        outcomes_path = self.root / "outcomes.json"
        outcomes_path.write_text(json.dumps(OUTCOMES), encoding="utf-8")

        return Fixture(
            root=self.root,
            config_path=config_path,
            inventory_path=inventory_path,
            candidates_path=candidates_path,
            outcomes_path=outcomes_path,
            registry_root=registry_root,
            work_root=work_root,
            resolution_root=self.root / "resolution",
        )


def _write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in columns})


def _fingerprint_digest(fingerprints: dict) -> str:
    """The resolver's canonical digest, so a test can forge a self-consistent record."""
    clean = {key: value for key, value in fingerprints.items() if key != "fingerprint_digest"}
    payload = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _receipt_of(fixture: Fixture) -> dict:
    return json.loads((fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME).read_text())


def _checkpoint(fixture: Fixture) -> Path:
    """The resolver records directory the candidate's run resolved into (#284)."""
    receipt = _receipt_of(fixture)
    return (
        Path(receipt["resolution_area"]) / "checkpoint" / receipt["resolver"]["settings_digest"]
    )


def _kept_record(fixture: Fixture, analysis_id: str) -> Path:
    """The kept Resolution Record the candidate's receipt names for one Analysis."""
    receipt = _receipt_of(fixture)
    return Path(receipt["resolution_area"]) / receipt["records"][analysis_id]["record"]


def _forge_kept_record(fixture: Fixture, analysis_id: str, edit) -> None:
    """Edit a kept record *and* re-bind the receipt to its new checksum.

    A plain edit is caught by the record checksum first; forging the checksum
    too shows the record-level checks still hold behind it.
    """
    receipt_path = fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
    receipt = json.loads(receipt_path.read_text())
    entry = receipt["records"][analysis_id]
    area = Path(receipt["resolution_area"])
    record = json.loads((area / entry["record"]).read_text())
    edit(record)
    data = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    entry["record_sha256"] = hashlib.sha256(data).hexdigest()
    entry["record"] = (
        f"records/{analysis_id}/{entry['fingerprint_digest']}/{entry['record_sha256']}.json"
    )
    (area / entry["record"]).write_bytes(data)
    receipt_path.write_text(json.dumps(receipt, indent=2))


def _two_resolver_rows() -> list[ResolverRow]:
    """Two minimal resolver rows for unit-level accounting tests."""
    return [
        ResolverRow("GCST1", "/tmp/a.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
                    "estimated_from_source_maf", "100", "a" * 64, "sha256", "10"),
        ResolverRow("GCST2", "/tmp/b.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
                    "estimated_from_source_maf", "200", "b" * 64, "sha256", "20"),
    ]


def _cli_invocation(
    fixture: Fixture,
    *args: str,
    cores: int = 1,
    outcomes: Path | None = None,
    fail_after: int | None = None,
    registry_root: Path | None = None,
    work_root: Path | None = None,
    resolution_root: Path | None = None,
    store_id: str = STORE_ID,
    config_path: Path | None = None,
    resolved_log: Path | None = None,
    resolver: str | Path = FAKE_RESOLVER,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The entry point's argv and environment for one test run."""
    command = [
        sys.executable,
        *CLI,
        store_id,
        "--config",
        str(config_path or fixture.config_path),
        "--cores",
        str(cores),
        "--resolver",
        str(resolver),
        "--repo-root",
        str(REPO_ROOT),
        "--registry-root",
        str(registry_root or fixture.registry_root),
        "--work-root",
        str(work_root or fixture.work_root),
        # Never the deployment default on /data: every test resolves into its
        # own temporary resolution root.
        "--resolution-root",
        str(resolution_root or fixture.resolution_root),
        *args,
    ]
    # The fake resolver is exec'd through its shebang, so put this interpreter's
    # directory first on PATH: it then runs under the same environment (and the
    # same installed opengwasdb) as the registry process.
    interpreter_dir = str(Path(sys.executable).resolve().parent)
    env = {
        **os.environ,
        "PATH": interpreter_dir + os.pathsep + os.environ.get("PATH", ""),
        "FAKE_RESOLVER_OUTCOMES": str(outcomes or fixture.outcomes_path),
        **(extra_env or {}),
    }
    if fail_after is not None:
        env["FAKE_RESOLVER_FAIL_AFTER"] = str(fail_after)
    if resolved_log is not None:
        env["FAKE_RESOLVER_LOG"] = str(resolved_log)
    return command, env


def _run_cli(fixture: Fixture, *args: str, **kwargs) -> subprocess.CompletedProcess:
    command, env = _cli_invocation(fixture, *args, **kwargs)
    return subprocess.run(
        command, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False
    )


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _candidate_tables_bytes(registry_root: Path) -> dict[str, bytes]:
    root = registry_root / STORE_ID
    return {
        "analyses.tsv": (root / "analyses.tsv").read_bytes(),
        "source_readiness.tsv": (root / "sidecars/source_readiness.tsv").read_bytes(),
        "ancestry.tsv": (root / "sidecars/ancestry.tsv").read_bytes(),
        "sd_estimation.tsv": (root / "sidecars/sd_estimation.tsv").read_bytes(),
        "exclusions.tsv": (root / "sidecars/exclusions.tsv").read_bytes(),
    }


def _synthetic_inventory_row(
    *, study_design: str = "quantitative", sample_size: str = "5000", readiness: str = "ok"
) -> SourceInventoryRow:
    return SourceInventoryRow(
        analysis_id="GCST90000001",
        publication_pmid="12345678",
        trait="Trait",
        study_design=study_design,
        sample_size=sample_size,
        readiness_status=readiness,
        data_url="https://example.invalid/x.h.tsv.gz",
        yaml_url="https://example.invalid/x.h.tsv.gz-meta.yaml",
        data_file="/mirror/GCST90000001.h.tsv.gz",
        yaml_file="/mirror/GCST90000001.h.tsv.gz-meta.yaml",
        data_bytes="100",
        yaml_bytes="10",
        sha256="a" * 64,
        error="",
    )


def _synthetic_candidate_metadata(
    *, sample_size: str = "5000", n_cases: str = "", n_controls: str = ""
) -> CandidateMetadata:
    return CandidateMetadata(
        analysis_id="GCST90000001",
        source_label="Trait",
        trait_ontology_label="",
        trait_ontology_id="",
        trait_ontology_mapping_method="unmapped",
        publication_pmid="12345678",
        first_author="Author",
        n_cases=n_cases,
        n_controls=n_controls,
        sample_size=sample_size,
    )


def _synthetic_resolver_record(
    *,
    gate_reason: str = "ok",
    eaf_orientation: str = "passed",
    eaf_orientation_r: float = 0.99,
    assigned_ancestry: str | None = "EUR",
    status: str = "success",
    phenotype_sd_status: str = "estimated",
    phenotype_sd_reason: str | None = None,
) -> dict:
    """A resolver record in the pinned real shape, including the EafOrientationOutcome."""
    estimate = (
        {"sd": 1.0, "dispersion": 0.05, "method": "estimated_from_source_maf", "notes": ""}
        if phenotype_sd_status == "estimated"
        else None
    )
    return {
        "record_schema_version": 1,
        "analysis_id": "GCST90000001",
        "status": status,
        "fingerprints": {},
        "diagnostics": {
            "source_file": "/mirror/GCST90000001.h.tsv.gz",
            "rows_read": 100,
            "ancestry_sites": 100,
        },
        "ancestry": {
            "assigned_ancestry": assigned_ancestry,
            "dominant_superpop": assigned_ancestry,
            "dominant_proportion": 0.9 if assigned_ancestry else 0.2,
            "runner_up_margin": 0.7 if assigned_ancestry else 0.1,
            "af_overlap": 100000,
            "residual": 0.01,
            "gate_reason": gate_reason,
            "eaf_orientation": eaf_orientation,
            "eaf_orientation_r": eaf_orientation_r,
            "superpop_composition": {assigned_ancestry: 0.9} if assigned_ancestry else {},
            "fine_composition": {},
        },
        "phenotype_sd": {
            "status": phenotype_sd_status,
            "reason": phenotype_sd_reason,
            "estimate": estimate,
            "reference_id": "",
            "n_evidence_considered": 100,
            "n_estimate_inputs": 90 if estimate else 0,
            "evidence_sampled": False,
        },
        "error": None if status == "success" else "controlled failure",
        "warnings": [],
        "metrics": {"elapsed_seconds": 0.0, "peak_memory_bytes": 0},
    }


class CandidateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase-b-candidate-")
        self.fixture = FixtureBuilder(Path(self._tmp.name)).build()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- happy path and policy -------------------------------------------------

    def test_full_pipeline_mixed_designs_and_controlled_outcomes(self) -> None:
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        bundle_dir = self.fixture.registry_root / STORE_ID
        self.assertTrue((bundle_dir / "release.yaml").is_file())
        self.assertTrue((bundle_dir / "build.yaml").is_file())
        self.assertTrue((bundle_dir / "validation.yaml").is_file())
        self.assertTrue((bundle_dir / "analyses.tsv").is_file())
        for sidecar in ("source_readiness", "ancestry", "sd_estimation", "exclusions", "reference_overlap"):
            self.assertTrue((bundle_dir / "sidecars" / f"{sidecar}.tsv").is_file())

        analyses = _read_tsv(bundle_dir / "analyses.tsv")
        included = {row["analysis_id"] for row in analyses if row["exclude_from_build"] != "true"}
        excluded = {
            row["analysis_id"]: row for row in analyses if row["exclude_from_build"] == "true"
        }
        self.assertEqual(included, EXPECTED_INCLUDED)
        self.assertEqual(set(excluded), set(EXPECTED_EXCLUDED))
        # The pinned GWAS-SSF reader has no validated imputation INFO/R²
        # projection; MAF and lookalike header spellings are not substitutes.
        self.assertEqual({row["info_score_threshold"] for row in analyses}, {"NaN"})

        # Case-control rows carry log_or/binary_trait and counts; quantitative
        # rows carry the computable SD tier.
        by_id = {row["analysis_id"]: row for row in analyses}
        self.assertEqual(by_id["GCST90000004"]["stored_effect_scale"], "log_or")
        self.assertEqual(by_id["GCST90000004"]["original_sd_method"], "binary_trait")
        self.assertEqual(by_id["GCST90000004"]["n_cases"], "4000")
        self.assertEqual(by_id["GCST90000004"]["n_controls"], "4000")
        self.assertEqual(by_id["GCST90000001"]["stored_effect_scale"], "sd")
        self.assertEqual(by_id["GCST90000001"]["original_sd_method"], "estimated_from_source_maf")
        self.assertEqual(by_id["GCST90000001"]["assigned_ancestry"], "EUR")
        self.assertNotEqual(by_id["GCST90000001"]["original_sd"], "")

        # Every excluded row carries its controlled reason in its audit row.
        for analysis_id, reason in EXPECTED_EXCLUDED.items():
            self.assertIn(reason, excluded[analysis_id]["inclusion_reason"])
            self.assertIn(reason, EXCLUSION_REASONS)

        exclusions = _read_tsv(bundle_dir / "sidecars/exclusions.tsv")
        reasons = {row["analysis_id"]: row["reason"] for row in exclusions}
        self.assertEqual(reasons, EXPECTED_EXCLUDED)

        # Every selected Analysis (included or excluded) has ancestry + SD rows.
        selected_ids = EXPECTED_INCLUDED | set(EXPECTED_EXCLUDED)
        ancestry = _read_tsv(bundle_dir / "sidecars/ancestry.tsv")
        sd_rows = _read_tsv(bundle_dir / "sidecars/sd_estimation.tsv")
        self.assertEqual({row["analysis_id"] for row in ancestry}, selected_ids)
        self.assertEqual({row["analysis_id"] for row in sd_rows}, selected_ids)
        overlap_rows = _read_tsv(bundle_dir / "sidecars/reference_overlap.tsv")
        self.assertEqual({row["analysis_id"] for row in overlap_rows}, selected_ids)
        self.assertEqual(
            {row["rows_scanned"] for row in overlap_rows if row["exclude_from_build"] != "true"},
            {"2000"},
        )
        sd_by_id = {row["analysis_id"]: row for row in sd_rows}
        self.assertEqual(sd_by_id["GCST90000004"]["status"], "skipped")
        self.assertEqual(
            sd_by_id["GCST90000004"]["skip_reason"], "non_quantitative_effect_scale"
        )
        self.assertEqual(sd_by_id["GCST90000003"]["status"], "failed")
        self.assertEqual(sd_by_id["GCST90000001"]["status"], "warning")

        # The 6,035-row-style inventory evidence stays distinct from membership.
        readiness = _read_tsv(bundle_dir / "sidecars/source_readiness.tsv")
        self.assertEqual(len(readiness), len(READY_ANALYSES) + 1)
        membership = {row["analysis_id"]: row["candidate_membership"] for row in readiness}
        self.assertEqual(membership["GCST90000010"], "not_ready")
        self.assertEqual(membership["GCST90000001"], "included")
        self.assertEqual(membership["GCST90000003"], "excluded")

        # Duplicate content is surfaced, not collapsed: both are included.
        duplicate_rows = {
            row["analysis_id"]: row["duplicate_content_group"]
            for row in readiness
            if row["duplicate_content_group"]
        }
        self.assertEqual(set(duplicate_rows), {"GCST90000001", "GCST90000002"})

        # release.yaml is a candidate that binds the frozen snapshot and the run.
        release = yaml.safe_load((bundle_dir / "release.yaml").read_text(encoding="utf-8"))
        self.assertEqual(release["status"], "candidate")
        self.assertEqual(release["source_snapshot_id"], SNAPSHOT_ID)
        self.assertEqual(release["source_snapshot"]["inventory_tsv_sha256"],
                         _sha256_bytes(self.fixture.inventory_path.read_bytes()))
        commands = " ".join(release["generator"]["commands"])
        self.assertIn("resolve-analyses", commands)
        self.assertIn("--n-workers 1", commands)
        self.assertIn("python3 -m manifest_generation.generate_candidate", commands)
        receipt_path = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        self.assertEqual(
            release["source_snapshot"]["resolver_receipt_sha256"],
            hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        )

        build = yaml.safe_load((bundle_dir / "build.yaml").read_text(encoding="utf-8"))
        self.assertEqual(build["layout"], "hybrid")
        self.assertEqual(build["completion_state"], "observed_only")
        self.assertEqual(build["build"]["command"], "build-hybrid")
        self.assertNotIn("artifacts", build)
        # Dense/Hybrid releases declare the maintained hm3 Indexed Variant Subset
        # by default (stores #210), sourced from the hapmap3-hg38 resource.
        self.assertEqual(
            build["indexed_subsets"],
            [{
                "name": "hm3",
                "resource_id": "hapmap3-hg38",
                "variant_list": (
                    "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt"
                ),
                "reference_assembly": "GRCh38",
                "checksum": (
                    "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398"
                ),
            }],
        )

        validation = yaml.safe_load((bundle_dir / "validation.yaml").read_text(encoding="utf-8"))
        self.assertIn(validation["status"], {"passed", "passed_with_warnings"})
        self.assertEqual(validation["checks"]["schema"], "passed")
        self.assertEqual(validation["checks"]["reference_overlap"], "passed")
        self.assertEqual(validation["reference_overlap"]["median_rate"], 1.0)

        # The complete bundle passes the executable contract, and the active
        # (non-excluded) analyses pass the pinned OpenGWASDB schema.
        loaded = bundle_module.load(STORE_ID, registry_root=self.fixture.registry_root)
        self.assertEqual(list(bundle_module.check(loaded, registry_root=self.fixture.registry_root)), [])
        self.assertEqual(validate_candidate_analyses((bundle_dir / "analyses.tsv").read_text()), [])
        # The observed-only Hybrid recipe is plannable exactly as ADR 0023 requires.
        steps = plan(loaded)
        self.assertEqual(steps[0].name, "build")
        self.assertIn("build-hybrid", steps[0].argv)
        step_names = [step.name for step in steps]
        self.assertIn("indexed-subset-hm3", step_names)
        self.assertLess(
            step_names.index("indexed-subset-hm3"),
            step_names.index("validate"),
        )

    def test_info_threshold_configuration_and_no_declaration_emission(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(config.info_score_threshold, "0.6")
        for value in ("0", "1", "0.6"):
            document = yaml.safe_load(self.fixture.config_path.read_text())
            document["defaults"]["info_score_threshold"] = value
            self.fixture.config_path.write_text(yaml.safe_dump(document))
            loaded = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
            self.assertEqual(loaded.info_score_threshold, value)
        # With no declaration configured every Analysis emits literal NaN, even
        # for an explicit zero request: the requested floor alone is not evidence.
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["defaults"]["info_score_threshold"] = 0
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        emitted = _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        self.assertEqual({row["info_score_threshold"] for row in emitted}, {"NaN"})
        self.assertEqual({row["imputation_score_column"] for row in emitted}, {""})

        for invalid in ("NaN", "inf", "-0.1", "1.1", "", True):
            document = yaml.safe_load(self.fixture.config_path.read_text())
            document["defaults"]["info_score_threshold"] = invalid
            self.fixture.config_path.write_text(yaml.safe_dump(document))
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "info_score_threshold"
            ):
                load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def _declare_scores(self, rows: list[dict[str, str]]) -> Path:
        path = self.fixture.root / "score-declarations.tsv"
        _write_tsv(path, list(candidate_workflow.SCORE_DECLARATION_COLUMNS), rows)
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["source"]["imputation_score_declarations"] = str(path)
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        return path

    def _set_outcome(self, analysis_id: str, **values: object) -> None:
        outcomes = json.loads(self.fixture.outcomes_path.read_text())
        outcomes.setdefault(analysis_id, {}).update(values)
        self.fixture.outcomes_path.write_text(json.dumps(outcomes))

    def _set_threshold(self, value: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["defaults"]["info_score_threshold"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_maf_threshold(self, value: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if value is None:
            document["defaults"].pop("maf_threshold", None)
        else:
            document["defaults"]["maf_threshold"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_maf_exempt(self, technologies: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if technologies is None:
            document["source"].pop("maf_filter_exempt_genotyping_technologies", None)
        else:
            document["source"]["maf_filter_exempt_genotyping_technologies"] = technologies
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_maf_exempt_analyses(self, path: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if path is None:
            document["source"].pop("maf_filter_exempt_analyses", None)
        else:
            document["source"]["maf_filter_exempt_analyses"] = path
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_store_composition(self, value: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if value is None:
            document.pop("store_composition", None)
        else:
            document["store_composition"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_indexed_subsets(self, value: object, *, present: bool = True) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if not present:
            document.pop("indexed_subsets", None)
        else:
            document["indexed_subsets"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_indexed_subsets_opt_out(self, value: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if value is None:
            document.pop("indexed_subsets_opt_out", None)
        else:
            document["indexed_subsets_opt_out"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _write_exemptions(
        self, rows: str, header: str = "analysis_id\treason\n"
    ) -> Path:
        path = self.fixture.root / "maf-filter-exemptions.tsv"
        path.write_text(header + rows, encoding="utf-8")
        return path

    def _write_metadata_yaml(self, name: str, technologies: object) -> Path:
        path = self.fixture.root / name
        document: dict[str, object] = {
            "genome_assembly": "GRCh38",
            "is_harmonised": True,
        }
        if technologies is not None:
            document["genotyping_technology"] = technologies
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return path

    def test_declared_usable_analysis_emits_threshold_and_triple(self) -> None:
        analysis_id = "GCST90000001"
        self._declare_scores([{
            "analysis_id": analysis_id,
            "imputation_score_column": "imputation_INFO",
            "imputation_score_kind": "imputation_info",
            "imputation_score_provenance": "provider specification: INFO is imputation quality",
        }])
        self._set_threshold(0.8)
        self._set_outcome(analysis_id, info_score_state="filtered", info_rows_usable=1500)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        by_id = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        }
        row = by_id[analysis_id]
        self.assertEqual(row["info_score_threshold"], "0.8")
        self.assertEqual(row["imputation_score_column"], "imputation_INFO")
        self.assertEqual(row["imputation_score_kind"], "imputation_info")
        self.assertEqual(
            row["imputation_score_provenance"],
            "provider specification: INFO is imputation quality",
        )
        # An undeclared Analysis gets literal NaN and empty triple cells.
        undeclared = by_id["GCST90000002"]
        self.assertEqual(undeclared["info_score_threshold"], "NaN")
        self.assertEqual(undeclared["imputation_score_column"], "")
        self.assertEqual(undeclared["imputation_score_kind"], "")
        self.assertEqual(undeclared["imputation_score_provenance"], "")

    def test_zero_threshold_with_usable_scores_emits_zero(self) -> None:
        analysis_id = "GCST90000001"
        self._declare_scores([{
            "analysis_id": analysis_id,
            "imputation_score_column": "imputation_INFO",
            "imputation_score_kind": "imputation_info",
            "imputation_score_provenance": "provider specification: INFO is imputation quality",
        }])
        self._set_threshold(0)
        self._set_outcome(analysis_id, info_score_state="disabled", info_rows_usable=1500)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        by_id = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        }
        # Zero is a real requested floor, emitted as "0", not treated as absence.
        self.assertEqual(by_id[analysis_id]["info_score_threshold"], "0")
        self.assertEqual(by_id[analysis_id]["imputation_score_column"], "imputation_INFO")

    # -- MAF threshold (issue #176) -------------------------------------------

    def test_maf_threshold_configuration_parsing(self) -> None:
        self.assertIsNone(parse_maf_threshold(None))
        self.assertEqual(parse_maf_threshold(0), "0")
        self.assertEqual(parse_maf_threshold("0"), "0")
        self.assertEqual(parse_maf_threshold(0.005), "0.005")
        self.assertEqual(parse_maf_threshold("0.5"), "0.5")
        for invalid in (True, "", "NaN", "inf", "-0.01", "0.5001", "abc"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "maf_threshold"
            ):
                parse_maf_threshold(invalid)
        self.assertEqual(parse_maf_filter_exempt_technologies(None), ())
        self.assertEqual(
            parse_maf_filter_exempt_technologies([" Whole genome sequencing "]),
            ("Whole genome sequencing",),
        )
        for invalid in ("Whole genome sequencing", [""], [1], ["ok", ""]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "maf_filter_exempt_genotyping_technologies"
            ):
                parse_maf_filter_exempt_technologies(invalid)

    def test_maf_threshold_is_per_analysis_from_metadata_yaml(self) -> None:
        # Omitted defaults.maf_threshold means no MAF filter at all.
        base = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertIsNone(base.maf_threshold)
        self.assertEqual(base.maf_filter_exempt_genotyping_technologies, ())

        self._set_maf_threshold(0.005)
        self._set_maf_exempt(["Whole genome sequencing", "Exome-wide sequencing"])
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(config.maf_threshold, "0.005")
        self.assertEqual(
            config.maf_filter_exempt_genotyping_technologies,
            ("Whole genome sequencing", "Exome-wide sequencing"),
        )
        missing = _synthetic_inventory_row()
        absent_yaml = self.fixture.root / "absent.yaml"
        self.assertEqual(
            read_genotyping_technologies(str(absent_yaml)), []
        )
        # Missing technology metadata is never an exemption.
        self.assertEqual(
            analysis_maf_threshold(replace(missing, yaml_file=str(absent_yaml)), config),
            "0.005",
        )
        exempt_path = self._write_metadata_yaml("wgs.yaml", ["Whole genome sequencing"])
        self.assertEqual(
            read_genotyping_technologies(str(exempt_path)), ["Whole genome sequencing"]
        )
        exempt = replace(missing, yaml_file=str(exempt_path))
        # Every technology in the exempt list -> NaN (no MAF filter).
        self.assertEqual(analysis_maf_threshold(exempt, config), "NaN")
        # An empty technology list is not "all in the list".
        empty_path = self._write_metadata_yaml("empty.yaml", [])
        self.assertEqual(
            analysis_maf_threshold(replace(missing, yaml_file=str(empty_path)), config),
            "0.005",
        )
        # One technology outside the list -> not exempt.
        mixed_path = self._write_metadata_yaml(
            "mixed.yaml", ["Whole genome sequencing", "Genome-wide genotyping array"]
        )
        self.assertEqual(
            analysis_maf_threshold(replace(missing, yaml_file=str(mixed_path)), config),
            "0.005",
        )
        # A no-MAF-filter release ignores exemptions entirely.
        self._set_maf_threshold(None)
        no_filter = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(analysis_maf_threshold(exempt, no_filter), "NaN")

    def test_maf_threshold_emission_is_per_analysis_evidence(self) -> None:
        declared = ResolverRow(
            "GCST1", "/tmp/a.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
            "estimated_from_source_maf", "100", "a" * 64, "sha256", "10",
            maf_threshold="0.005",
        )
        matching = {
            "diagnostics": {"maf_state": "filtered"},
            "fingerprints": {"resolution_config": {"maf_threshold": 0.005}},
        }
        self.assertEqual(maf_threshold_emission(declared, matching), "0.005")
        self.assertEqual(maf_threshold_emission(None, matching), "NaN")
        self.assertEqual(
            maf_threshold_emission(replace(declared, maf_threshold="NaN"), matching),
            "NaN",
        )
        # A record that did not apply MAF (unavailable), or one without the
        # #176 diagnostics, has no evidence: NaN.
        unavailable = {
            "diagnostics": {"maf_state": "unavailable"},
            "fingerprints": {"resolution_config": {"maf_threshold": 0.005}},
        }
        self.assertEqual(maf_threshold_emission(declared, unavailable), "NaN")
        self.assertEqual(maf_threshold_emission(declared, {"diagnostics": {}}), "NaN")
        # A fingerprint that no longer binds the value emits NaN.
        stale = {
            "diagnostics": {"maf_state": "filtered"},
            "fingerprints": {"resolution_config": {"maf_threshold": 0.01}},
        }
        self.assertEqual(maf_threshold_emission(declared, stale), "NaN")
        # Zero is a real requested floor, emitted as "0", not treated as absence.
        zero = replace(declared, maf_threshold="0")
        disabled = {
            "diagnostics": {"maf_state": "disabled"},
            "fingerprints": {"resolution_config": {"maf_threshold": 0}},
        }
        self.assertEqual(maf_threshold_emission(zero, disabled), "0")
        self.assertEqual(USABLE_MAF_STATES, frozenset({"disabled", "filtered"}))

    def test_maf_threshold_manifest_receipt_and_candidate_emission(self) -> None:
        self._set_maf_threshold(0.005)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        self.assertIn("maf_threshold", RESOLVER_MANIFEST_COLUMNS)
        manifest = _read_tsv(run_root / "analyses.tsv")
        self.assertEqual({row["maf_threshold"] for row in manifest}, {"0.005"})
        record = json.loads(_kept_record(self.fixture, "GCST90000001").read_text())
        self.assertEqual(
            record["fingerprints"]["resolution_config"]["maf_threshold"], 0.005
        )
        self.assertEqual(record["diagnostics"]["maf_state"], "filtered")
        emitted = _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        self.assertEqual({row["maf_threshold"] for row in emitted}, {"0.005"})
        receipt = read_resolution_receipt(run_root / RECEIPT_FILENAME)
        self.assertEqual(receipt["contract"]["maf_threshold"], "0.005")
        self.assertEqual(
            receipt["contract"]["maf_filter_exempt_genotyping_technologies"], []
        )

    def test_maf_threshold_requires_resolver_fingerprint(self) -> None:
        self._set_maf_threshold(0.005)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        def _drop(record: dict) -> None:
            del record["fingerprints"]["resolution_config"]['maf_threshold']
            record["fingerprints"]["fingerprint_digest"] = _fingerprint_digest(
                record["fingerprints"]
            )

        record_path = _checkpoint(self.fixture) / "GCST90000001.json"
        record = json.loads(record_path.read_text())
        _drop(record)
        record_path.write_text(json.dumps(record))
        _forge_kept_record(self.fixture, "GCST90000001", _drop)
        inventory = read_inventory(self.fixture.inventory_path)
        metadata = read_candidate_metadata(
            self.fixture.candidates_path,
            (row.analysis_id for row in inventory if row.ready),
        )
        manifest = derive_resolver_manifest(
            inventory,
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            metadata,
        )
        _, failures = account_records(manifest, _checkpoint(self.fixture))
        self.assertTrue(
            any("resolution_config.maf_threshold" in error for error in failures), failures
        )
        stale = _run_cli(self.fixture, "--stage", "emit")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("resolution_config.maf_threshold", stale.stderr + stale.stdout)

    def test_changed_maf_threshold_makes_the_receipt_stale(self) -> None:
        self._set_maf_threshold(0.005)
        self._resolved_candidate()
        self._set_maf_threshold(0.01)
        self._assert_emit_rejects_and_preserves("resolution contract changed")

    def test_maf_filter_exemption_file_parsing(self) -> None:
        # An absent path is no exemptions, never an error.
        self.assertEqual(read_maf_filter_exempt_analyses(None), {})
        good = self._write_exemptions(
            "GCST90428462\tplaceholder frequency one\n"
            "GCST90428463\tplaceholder frequency two\n"
        )
        self.assertEqual(
            read_maf_filter_exempt_analyses(good),
            {
                "GCST90428462": "placeholder frequency one",
                "GCST90428463": "placeholder frequency two",
            },
        )
        duplicate = self._write_exemptions("GCST1\tone\nGCST1\ttwo\n")
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(duplicate)
        self.assertIn(f"{duplicate}:3: duplicate analysis_id", str(caught.exception))
        blank_id = self._write_exemptions("\treason\n")
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(blank_id)
        self.assertIn(f"{blank_id}:2: analysis_id must be non-empty", str(caught.exception))
        blank_reason = self._write_exemptions("GCST1\t \n")
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(blank_reason)
        self.assertIn(f"{blank_reason}:2: reason must be non-empty", str(caught.exception))
        bad_header = self._write_exemptions(
            "GCST1\treason\n", header="analysis_id\trationale\n"
        )
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(bad_header)
        self.assertIn(f"{bad_header}: expected exact TSV headers", str(caught.exception))
        missing = self.fixture.root / "absent-exemptions.tsv"
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(missing)
        self.assertIn(f"not found: {missing}", str(caught.exception))

    def test_committed_maf_filter_exemptions_resolve_against_the_repo_root(self) -> None:
        # `source.maf_filter_exempt_analyses` is a repo-root-relative path and the
        # committed table records the operator's reason for each exemption.
        self._set_maf_exempt_analyses(
            "resources/generators/gwas-catalog-eur-hybrid/maf-filter-exemptions.tsv"
        )
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(
            config.maf_filter_exempt_analyses_path,
            REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/maf-filter-exemptions.tsv",
        )
        self.assertEqual(
            set(config.maf_filter_exempt_analyses), {"GCST90428462", "GCST90428463"}
        )
        for analysis_id in ("GCST90428462", "GCST90428463"):
            reason = config.maf_filter_exempt_analyses[analysis_id]
            self.assertIn("effect_allele_frequency is 0.0 on every row", reason)
            self.assertIn("minor_allele_freq_lower_limit 0.01", reason)

    def test_listed_analysis_emits_nan_maf_threshold_in_manifest_and_receipt(self) -> None:
        self._set_maf_threshold(0.005)
        exempt = self._write_exemptions("GCST90000001\tplaceholder frequency (fixture)\n")
        self._set_maf_exempt_analyses(str(exempt))
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        metadata = read_candidate_metadata(
            self.fixture.candidates_path, (row.analysis_id for row in rows if row.ready)
        )
        manifest = derive_resolver_manifest(rows, config, metadata)
        by_id = {row.analysis_id: row for row in manifest}
        # The exempt Analysis carries the NaN the manifest already understands;
        # every other Analysis keeps the configured floor.
        self.assertEqual(by_id["GCST90000001"].maf_threshold, "NaN")
        self.assertEqual(by_id["GCST90000002"].maf_threshold, "0.005")
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        emitted = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        }
        # No resolver evidence for an exempt Analysis -> literal NaN, as for an
        # exempt genotyping technology.
        self.assertEqual(emitted["GCST90000001"]["maf_threshold"], "NaN")
        self.assertEqual(emitted["GCST90000002"]["maf_threshold"], "0.005")
        receipt = read_resolution_receipt(
            self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        )
        self.assertEqual(receipt["contract"]["maf_filter_exempt_analyses"], str(exempt))
        self.assertEqual(
            receipt["contract"]["maf_filter_exempt_analyses_reasons"],
            {"GCST90000001": "placeholder frequency (fixture)"},
        )

    def test_changed_maf_exemption_makes_the_receipt_stale(self) -> None:
        self._set_maf_threshold(0.005)
        exempt = self._write_exemptions("GCST90000001\tfirst reason\n")
        self._set_maf_exempt_analyses(str(exempt))
        self._resolved_candidate()
        self._write_exemptions("GCST90000001\tsecond reason\n")
        self._assert_emit_rejects_and_preserves("resolution contract changed")

    def test_declared_analysis_with_no_usable_scores_is_included_with_nan_info(self) -> None:
        # Core no longer treats zero usable scores as a controlled failure, and
        # the registry no longer refuses the Analysis (#176): every row is
        # retained and the emitted INFO cells are literal NaN / empty.
        analysis_id = "GCST90000001"
        self._declare_scores([{
            "analysis_id": analysis_id,
            "imputation_score_column": "imputation_INFO",
            "imputation_score_kind": "imputation_info",
            "imputation_score_provenance": "provider specification: INFO is imputation quality",
        }])
        self._set_outcome(analysis_id, info_rows_usable=0)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        exclusions = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "sidecars/exclusions.tsv")
        }
        self.assertNotIn(analysis_id, exclusions)
        by_id = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        }
        self.assertEqual(by_id[analysis_id]["exclude_from_build"], "")
        self.assertEqual(by_id[analysis_id]["info_score_threshold"], "NaN")
        self.assertEqual(by_id[analysis_id]["imputation_score_column"], "")
        self.assertEqual(by_id[analysis_id]["imputation_score_kind"], "")
        self.assertEqual(by_id[analysis_id]["imputation_score_provenance"], "")

    def test_info_score_emission_is_per_analysis_evidence(self) -> None:
        declared = ResolverRow(
            "GCST1", "/tmp/a.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
            "estimated_from_source_maf", "100", "a" * 64, "sha256", "10",
            info_score_threshold="0.6", imputation_score_column="imputation_INFO",
            imputation_score_kind="imputation_info",
            imputation_score_provenance="provider: INFO quality",
        )
        matching_fingerprint = {
            "resolution_config": {
                "info_score_threshold": 0.6,
                "imputation_score_column": "imputation_INFO",
                "imputation_score_kind": "imputation_info",
                "imputation_score_provenance": "provider: INFO quality",
            }
        }
        usable = {
            "diagnostics": {"info_score_state": "filtered", "info_rows_usable": 5},
            "fingerprints": matching_fingerprint,
        }
        self.assertEqual(
            info_score_emission(declared, usable),
            ("0.6", "imputation_INFO", "imputation_info", "provider: INFO quality"),
        )
        # Undeclared: NaN whatever the record says.
        undeclared = replace(
            declared, info_score_threshold="NaN", imputation_score_column="",
            imputation_score_kind="", imputation_score_provenance="",
        )
        self.assertEqual(info_score_emission(undeclared, usable), ("NaN", "", "", ""))
        # Legacy record without the #175 diagnostics: NaN, never read as a pass.
        self.assertEqual(
            info_score_emission(declared, {"diagnostics": {"rows_read": 100}}),
            ("NaN", "", "", ""),
        )
        # Declared but no usable score: NaN.
        self.assertEqual(
            info_score_emission(
                declared,
                {"diagnostics": {"info_score_state": "filtered", "info_rows_usable": 0},
                 "fingerprints": matching_fingerprint},
            ),
            ("NaN", "", "", ""),
        )
        # Declared and usable, but the fingerprint no longer binds it: NaN.
        stale = {
            "diagnostics": {"info_score_state": "filtered", "info_rows_usable": 5},
            "fingerprints": {"resolution_config": {"info_score_threshold": 0.6}},
        }
        self.assertEqual(info_score_emission(declared, stale), ("NaN", "", "", ""))

    def test_declared_score_manifest_receipt_and_candidate_handoff(self) -> None:
        analysis_id = "GCST90000004"
        path = self._declare_scores([{
            "analysis_id": analysis_id,
            "imputation_score_column": "imputation_INFO",
            "imputation_score_kind": "imputation_info",
            "imputation_score_provenance": "provider specification: INFO is imputation quality",
        }])
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["defaults"]["info_score_threshold"] = 0
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        manifest = _read_tsv(run_root / "analyses.tsv")
        self.assertEqual(tuple(manifest[0]), RESOLVER_MANIFEST_COLUMNS)
        by_id = {row["analysis_id"]: row for row in manifest}
        declared = by_id[analysis_id]
        self.assertEqual(declared["info_score_threshold"], "0")
        self.assertEqual(declared["imputation_score_column"], "imputation_INFO")
        self.assertEqual(declared["imputation_score_kind"], "imputation_info")
        self.assertEqual(declared["imputation_score_provenance"],
                         "provider specification: INFO is imputation quality")
        self.assertEqual(by_id["GCST90000001"]["info_score_threshold"], "NaN")
        self.assertEqual(by_id["GCST90000001"]["imputation_score_column"], "")
        record = json.loads(_kept_record(self.fixture, analysis_id).read_text())
        self.assertEqual(record["fingerprints"]["resolution_config"]["imputation_score_column"],
                         "imputation_INFO")
        receipt = read_resolution_receipt(run_root / RECEIPT_FILENAME)
        self.assertEqual(receipt["contract"]["imputation_score_declarations_sha256"],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(receipt["manifest_sha256"],
                         hashlib.sha256((run_root / "analyses.tsv").read_bytes()).hexdigest())
        final = _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        by_id = {row["analysis_id"]: row for row in final}
        # An approved declaration with usable resolver evidence emits the exact
        # requested floor and triple; the undeclared Analysis stays NaN.
        self.assertEqual(by_id[analysis_id]["info_score_threshold"], "0")
        self.assertEqual(by_id[analysis_id]["imputation_score_column"], "imputation_INFO")
        self.assertEqual(by_id[analysis_id]["imputation_score_kind"], "imputation_info")
        self.assertEqual(
            by_id[analysis_id]["imputation_score_provenance"],
            "provider specification: INFO is imputation quality",
        )
        self.assertEqual(by_id["GCST90000001"]["info_score_threshold"], "NaN")
        self.assertEqual(by_id["GCST90000001"]["imputation_score_column"], "")
        self.assertEqual(_run_cli(self.fixture, "--stage", "verify").returncode, 0)
        path.write_text(path.read_text().replace("imputation_INFO", "imputation_R2"))
        stale = _run_cli(self.fixture, "--stage", "verify")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("resolution contract changed", stale.stderr + stale.stdout)

    def test_declared_score_requires_resolver_fingerprint(self) -> None:
        analysis_id = "GCST90000004"
        self._declare_scores([{
            "analysis_id": analysis_id, "imputation_score_column": "imputation_r2",
            "imputation_score_kind": "imputation_r2",
            "imputation_score_provenance": "provider dictionary: imputation r2",
        }])
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        # Both the checkpoint record a fresh run accounts and the kept record a
        # later emit reads lose the key.
        def _drop(record: dict) -> None:
            del record["fingerprints"]["resolution_config"]['imputation_score_kind']
            record["fingerprints"]["fingerprint_digest"] = _fingerprint_digest(
                record["fingerprints"]
            )

        record_path = _checkpoint(self.fixture) / f"{analysis_id}.json"
        record = json.loads(record_path.read_text())
        _drop(record)
        record_path.write_text(json.dumps(record))
        _forge_kept_record(self.fixture, analysis_id, _drop)
        inventory = read_inventory(self.fixture.inventory_path)
        metadata = read_candidate_metadata(self.fixture.candidates_path,
                                           (row.analysis_id for row in inventory if row.ready))
        manifest = derive_resolver_manifest(inventory, load_candidate_configuration(
            self.fixture.config_path, REPO_ROOT), metadata)
        _, failures = account_records(manifest, _checkpoint(self.fixture))
        self.assertTrue(any("resolution_config.imputation_score_kind" in error
                            for error in failures), failures)
        stale = _run_cli(self.fixture, "--stage", "emit")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("resolution_config.imputation_score_kind", stale.stderr + stale.stdout)

    def test_score_declaration_validation_and_legacy_manifest(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        metadata = read_candidate_metadata(self.fixture.candidates_path,
                                           (row.analysis_id for row in rows if row.ready))
        legacy = derive_resolver_manifest(rows, config, metadata)
        self.assertEqual({row.info_score_threshold for row in legacy}, {"NaN"})
        self.assertEqual({row.imputation_score_kind for row in legacy}, {""})
        valid = {"analysis_id": "GCST90000001", "imputation_score_column": "r2",
                 "imputation_score_kind": "imputation_r2",
                 "imputation_score_provenance": "provider data dictionary: imputation R2"}
        for broken, message in (
            ([{**valid, "analysis_id": "unknown"}], "unknown analysis_id"),
            ([valid, valid], "duplicate analysis_id"),
            ([{**valid, "imputation_score_column": " r2"}], "exact non-empty"),
            ([{**valid, "imputation_score_kind": "MAF"}], "invalid imputation_score_kind"),
            ([{**valid, "imputation_score_provenance": ""}], "independent provider evidence"),
        ):
            with self.subTest(broken=broken):
                self._declare_scores(broken)
                configured = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
                with self.assertRaisesRegex(CandidateError, message):
                    derive_resolver_manifest(rows, configured, metadata)
        path = self._declare_scores([valid])
        path.write_text(path.read_text().replace("imputation_score_kind", "score_kind"))
        with self.assertRaisesRegex(CandidateError, "exact TSV headers"):
            derive_resolver_manifest(rows, load_candidate_configuration(
                self.fixture.config_path, REPO_ROOT), metadata)
        path.unlink()
        with self.assertRaisesRegex(CandidateError, "table not found"):
            derive_resolver_manifest(rows, load_candidate_configuration(
                self.fixture.config_path, REPO_ROOT), metadata)

    def test_worker_count_byte_equivalence(self) -> None:
        single_root = self.fixture.root / "stores-1"
        multi_root = self.fixture.root / "stores-4"
        single_root.mkdir()
        multi_root.mkdir()
        one = _run_cli(
            self.fixture, cores=1, registry_root=single_root, work_root=self.fixture.root / "work-1",
            resolution_root=self.fixture.root / "resolution-1",
        )
        self.assertEqual(one.returncode, 0, one.stderr)
        four = _run_cli(
            self.fixture, cores=4, registry_root=multi_root, work_root=self.fixture.root / "work-4",
            resolution_root=self.fixture.root / "resolution-4",
        )
        self.assertEqual(four.returncode, 0, four.stderr)
        self.assertEqual(_candidate_tables_bytes(single_root), _candidate_tables_bytes(multi_root))

    def test_resume_reuses_records_and_reproduces_bytes(self) -> None:
        first = _run_cli(self.fixture)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = _candidate_tables_bytes(self.fixture.registry_root)

        second = _run_cli(self.fixture)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("--resume", second.stdout)
        after = _candidate_tables_bytes(self.fixture.registry_root)
        self.assertEqual(before, after)

        index = json.loads(
            (self.fixture.work_root / STORE_ID / "resolver/index.json").read_text()
        )
        self.assertGreater(index["n_resumed"], 0)

    def test_kill_then_resume_matches_uninterrupted_run(self) -> None:
        ok_root = self.fixture.root / "stores-ok"
        ok_root.mkdir()
        ok = _run_cli(
            self.fixture, registry_root=ok_root, work_root=self.fixture.root / "work-ok",
            resolution_root=self.fixture.root / "resolution-ok",
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        expected = _candidate_tables_bytes(ok_root)

        resume_root = self.fixture.root / "stores-resume"
        resume_root.mkdir()
        resume_work = self.fixture.root / "work-resume"
        killed = _run_cli(
            self.fixture,
            registry_root=resume_root,
            work_root=resume_work,
            resolution_root=self.fixture.root / "resolution-resume",
            fail_after=3,
        )
        self.assertNotEqual(killed.returncode, 0)
        self.assertFalse((resume_root / STORE_ID).exists())

        resumed = _run_cli(
            self.fixture,
            registry_root=resume_root,
            work_root=resume_work,
            resolution_root=self.fixture.root / "resolution-resume",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(_candidate_tables_bytes(resume_root), expected)
        index = json.loads(
            (resume_work / STORE_ID / "resolver/index.json").read_text()
        )
        self.assertEqual(index["n_resumed"], 3)

    def test_re_emitting_the_candidate_is_byte_identical_in_every_file(self) -> None:
        """`--stage emit` over its own candidate rewrites every file byte for byte (#248)."""
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        bundle_dir = self.fixture.registry_root / STORE_ID

        def every_file() -> dict[str, bytes]:
            return {str(p.relative_to(bundle_dir)): p.read_bytes() for p in sorted(bundle_dir.rglob("*")) if p.is_file()}

        pinned = ("--stage", "emit", "--created-at", "2026-10-14T00:00:00Z")
        first = _run_cli(self.fixture, *pinned)
        self.assertEqual(first.returncode, 0, first.stderr)
        emitted = every_file()
        second = _run_cli(self.fixture, *pinned)
        self.assertEqual(second.returncode, 0, second.stderr)

        self.assertIn("release.yaml", emitted)
        self.assertIn("summary.yaml", emitted)
        self.assertEqual(every_file(), emitted)

    def test_interrupted_run_preserves_prior_candidate(self) -> None:
        first = _run_cli(self.fixture)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = _candidate_tables_bytes(self.fixture.registry_root)
        release_before = (self.fixture.registry_root / STORE_ID / "release.yaml").read_bytes()

        # A changed gate forces every Analysis to be re-resolved, so the
        # interruption lands mid-run rather than among reused records.
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["ancestry_assignment"]["gates"]["tau"] = 0.6
        self.fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False))
        interrupted = _run_cli(self.fixture, fail_after=2)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertIn("resolver exited", interrupted.stderr)

        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)
        self.assertEqual(
            (self.fixture.registry_root / STORE_ID / "release.yaml").read_bytes(), release_before
        )

    def test_failed_finalisation_does_not_create_a_candidate(self) -> None:
        result = _run_cli(self.fixture, fail_after=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())

    # -- accounting ------------------------------------------------------------

    def _run_emit_only(self) -> subprocess.CompletedProcess:
        return _run_cli(self.fixture, "--stage", "emit")

    def test_missing_record_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        before = _candidate_tables_bytes(self.fixture.registry_root)
        _kept_record(self.fixture, "GCST90000002").unlink()
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stderr)
        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)

    def test_another_selections_record_in_the_checkpoint_is_not_a_failure(self) -> None:
        # The checkpoint is shared by every Selection of the collection (#284);
        # a receipt naming a record outside its manifest is what fails
        # (SharedResolutionRecordsTests).
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        (_checkpoint(self.fixture) / "GCST99999999.json").write_text("{}", encoding="utf-8")
        result = self._run_emit_only()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_record_schema_version_mismatch_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        _forge_kept_record(
            self.fixture, "GCST90000001", lambda r: r.__setitem__("record_schema_version", 2)
        )
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("record_schema_version", result.stderr)

    def test_index_schema_version_mismatch_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        index_path = self.fixture.work_root / STORE_ID / "resolver/index.json"
        index = json.loads(index_path.read_text())
        index["record_schema_version"] = 2
        index_path.write_text(json.dumps(index), encoding="utf-8")
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("record_schema_version", result.stderr)

    # -- stale-contract (resolution receipt) -----------------------------------

    def _resolved_candidate(self) -> None:
        """Produce a successful candidate whose records and receipt are on disk."""
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr)

    def _assert_emit_rejects_and_preserves(self, expected_message: str) -> None:
        before = _candidate_tables_bytes(self.fixture.registry_root)
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected_message, result.stderr)
        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)

    def test_changed_gate_makes_the_receipt_stale(self) -> None:
        self._resolved_candidate()
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["ancestry_assignment"]["gates"]["tau"] = 0.9
        self.fixture.config_path.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        self._assert_emit_rejects_and_preserves("resolution contract changed")

    def test_changed_ancestry_reference_makes_the_receipt_stale(self) -> None:
        self._resolved_candidate()
        reference = self.fixture.root / "reference/ref_freqs.tsv.gz"
        reference.write_bytes(reference.read_bytes() + b"# changed content\n")
        self._assert_emit_rejects_and_preserves("ancestry_reference_sha256")

    def test_changed_ancestry_groups_makes_the_receipt_stale(self) -> None:
        self._resolved_candidate()
        groups = self.fixture.root / "reference/ancestry_groups.tsv"
        groups.write_text(
            groups.read_text(encoding="utf-8") + "AFR_A\tAFR\n", encoding="utf-8"
        )
        self._assert_emit_rejects_and_preserves("ancestry_groups_sha256")

    def test_missing_receipt_fails_before_replacement(self) -> None:
        self._resolved_candidate()
        receipt = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt.unlink()
        self._assert_emit_rejects_and_preserves("resolution receipt is missing")

    def test_receipt_binds_every_record_digest(self) -> None:
        self._resolved_candidate()
        receipt = json.loads(
            (self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME).read_text()
        )
        self.assertEqual(list(receipt["records"]), receipt["analysis_ids"])
        for analysis_id in receipt["analysis_ids"]:
            record_path = _kept_record(self.fixture, analysis_id)
            record = json.loads(record_path.read_text())
            entry = receipt["records"][analysis_id]
            self.assertEqual(
                entry["fingerprint_digest"], record["fingerprints"]["fingerprint_digest"]
            )
            self.assertEqual(
                entry["record_sha256"], hashlib.sha256(record_path.read_bytes()).hexdigest()
            )
        self.assertIn("resolve-analyses", " ".join(receipt["resolver"]["argv"]))

    def test_tampered_fingerprint_with_valid_self_digest_is_rejected(self) -> None:
        # The exact hole review found: mutate a gate/evidence fingerprint input
        # and recompute a *valid* self-digest. The record is internally
        # consistent, so only the receipt can reject it.
        self._resolved_candidate()
        record_path = _kept_record(self.fixture, "GCST90000001")
        record = json.loads(record_path.read_text())
        record["fingerprints"]["resolution_config"]["gates"]["tau"] = 0.99
        record["fingerprints"]["resolution_config"]["evidence_sample"] = 123
        record["fingerprints"]["opengwasdb_git_hash"] = "a-different-revision"
        record["fingerprints"]["fingerprint_digest"] = _fingerprint_digest(
            record["fingerprints"]
        )
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        self._assert_emit_rejects_and_preserves("bound")

    def test_changed_tool_identity_makes_the_receipt_stale(self) -> None:
        self._resolved_candidate()
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        manifest = derive_resolver_manifest(
            read_inventory(self.fixture.inventory_path),
            config,
            read_candidate_metadata(
                self.fixture.candidates_path,
                [row.analysis_id for row in read_inventory(self.fixture.inventory_path) if row.ready],
            ),
        )
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        with mock.patch.object(
            candidate_workflow,
            "_opengwasdb_tool_identity",
            return_value=("0.3.0", "0" * 64),
        ):
            _, failures = verify_records(
                manifest,
                config=config,
                manifest_path=run_root / "analyses.tsv",
                receipt_path=run_root / RECEIPT_FILENAME,
                index_path=run_root / "index.json",
            )
        self.assertTrue(
            any("opengwasdb_resolver_sha256" in failure for failure in failures), failures
        )

    def test_stale_record_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        _forge_kept_record(
            self.fixture,
            "GCST90000001",
            lambda r: r["fingerprints"].__setitem__("source_recorded_sha256", "0" * 64),
        )
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fingerprint", result.stderr)

    def test_account_records_flags_missing_duplicate_and_extra(self) -> None:
        rows = _two_resolver_rows()
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp)
            base_index = {
                "record_schema_version": 1,
                "n_total": 2,
                "analyses": [
                    {"analysis_id": "GCST1", "status": "success"},
                    {"analysis_id": "GCST2", "status": "success"},
                ],
            }
            # index present but a record missing
            (records / "index.json").write_text(json.dumps(base_index))
            _, failures = account_records(rows, records)
            self.assertTrue(any("missing" in failure for failure in failures))

            # duplicate id in the index
            duplicate_index = dict(base_index)
            duplicate_index["analyses"] = [
                {"analysis_id": "GCST1", "status": "success"},
                {"analysis_id": "GCST1", "status": "success"},
            ]
            (records / "index.json").write_text(json.dumps(duplicate_index))
            _, failures = account_records(rows, records)
            self.assertTrue(any("duplicate analysis_id" in failure for failure in failures))

            # A record the manifest does not name is another Selection's: the
            # checkpoint is shared by the collection (#284).
            (records / "index.json").write_text(json.dumps(base_index))
            (records / "GCST99999999.json").write_text("{}")
            _, failures = account_records(rows, records)
            self.assertFalse(any("GCST99999999" in failure for failure in failures), failures)

    def test_account_records_rejects_incompatible_schema_versions(self) -> None:
        rows = _two_resolver_rows()[:1]
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp)
            (records / "index.json").write_text(
                json.dumps(
                    {
                        "record_schema_version": 2,
                        "n_total": 1,
                        "analyses": [{"analysis_id": "GCST1", "status": "success"}],
                    }
                )
            )
            _, failures = account_records(rows, records)
            self.assertTrue(any("record_schema_version" in failure for failure in failures))

            (records / "index.json").write_text(
                json.dumps(
                    {
                        "record_schema_version": 1,
                        "n_total": 1,
                        "analyses": [{"analysis_id": "GCST1", "status": "success"}],
                    }
                )
            )
            (records / "GCST1.json").write_text(
                json.dumps(
                    {
                        "record_schema_version": 2,
                        "analysis_id": "GCST1",
                        "status": "success",
                    }
                )
            )
            _, failures = account_records(rows, records)
            self.assertTrue(any("record_schema_version" in failure for failure in failures))

    def test_read_resolution_receipt_rejects_incompatible_schema_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / RECEIPT_FILENAME
            path.write_text(json.dumps({"receipt_schema_version": 3}))
            with self.assertRaises(CandidateError):
                read_resolution_receipt(path)
            path.write_text(
                json.dumps({"receipt_schema_version": 2, "record_schema_version": 9})
            )
            with self.assertRaises(CandidateError):
                read_resolution_receipt(path)
            with self.assertRaises(CandidateError):
                read_resolution_receipt(Path(tmp) / "missing.json")

    # -- units -----------------------------------------------------------------

    def test_resolver_argv_composes_declared_facts(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        argv = resolver_argv(
            resolver_bin="opengwasdb",
            manifest_path=Path("/tmp/analyses.tsv"),
            records_dir=Path("/tmp/records"),
            config=config,
            cores=64,
        )
        self.assertEqual(argv[:2], ["opengwasdb", "resolve-analyses"])
        for flag in (
            "--ancestry-reference",
            "--ancestry-groups",
            "--default-source-reader-capability",
            "--maf-floor",
            "--tau",
            "--delta",
            "--n-min",
            "--residual-max",
            "--n-workers",
            "--resume",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--n-workers") + 1], "64")
        self.assertNotIn("--af-reference", argv)  # source-AF-only policy (#152)

    def test_resolver_manifest_uses_exact_inventory_paths(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        metadata = read_candidate_metadata(
            self.fixture.candidates_path, [row.analysis_id for row in rows if row.ready]
        )
        manifest = derive_resolver_manifest(rows, config, metadata)
        self.assertEqual(len(manifest), len(READY_ANALYSES))
        rendered = render_resolver_manifest(manifest)
        header = rendered.splitlines()[0].split("\t")
        self.assertEqual(tuple(header), RESOLVER_MANIFEST_COLUMNS)
        inventory_by_id = {row.analysis_id: row for row in rows}
        for entry in manifest:
            self.assertEqual(entry.source_file, inventory_by_id[entry.analysis_id].data_file)
            self.assertEqual(entry.checksum, inventory_by_id[entry.analysis_id].sha256)

    def test_apply_release_policy_is_total_and_controlled(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        ready_ids = [row.analysis_id for row in rows if row.ready]
        metadata = read_candidate_metadata(self.fixture.candidates_path, ready_ids)
        manifest = derive_resolver_manifest(rows, config, metadata)
        # Synthesise records straight from the manifest, using the fake's shape.
        run = _run_cli(self.fixture, "--stage", "resolve")
        self.assertEqual(run.returncode, 0, run.stderr)
        records, failures = verify_records(
            manifest,
            config=config,
            manifest_path=self.fixture.work_root / STORE_ID / "resolver/analyses.tsv",
            receipt_path=self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME,
            index_path=self.fixture.work_root / STORE_ID / "resolver/index.json",
        )
        self.assertEqual(failures, [])
        index = json.loads(
            (self.fixture.work_root / STORE_ID / "resolver/index.json").read_text()
        )
        outcomes = apply_release_policy(
            rows, config, metadata, {record["analysis_id"]: record for record in records}
        )
        self.assertEqual(
            {outcome.analysis_id for outcome in outcomes if outcome.included},
            EXPECTED_INCLUDED,
        )
        self.assertTrue(
            all(
                outcome.exclusion_reason in EXCLUSION_REASONS
                for outcome in outcomes
                if not outcome.included
            )
        )
        tables = build_candidate_tables(
            inventory_rows=rows, outcomes=outcomes, config=config, index_summary=index
        )
        self.assertEqual(tables.included_rows, len(EXPECTED_INCLUDED))
        self.assertEqual(dict(tables.exclusion_counts), {
            "ancestry_not_eur": 1,
            "ancestry_unassigned": 1,
            "missing_case_control_counts": 1,
            "orientation_failure": 1,
            "resolution_failed": 1,
            "sd_no_qualifying_evidence": 1,
        })
        self.assertEqual(tables.ancestry_check, "passed_with_warnings")
        self.assertEqual(tables.sd_check, "passed_with_warnings")

    def _declare_variant_reference(self) -> None:
        """Declare a Variant Reference that exists, as the resolver needs it to."""
        axis = self.fixture.root / "reference" / "axis.gz"
        axis.write_bytes(b"fixture variant reference\n")
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config["build"]["options"]["variant-reference"] = str(axis)
        self.fixture.config_path.write_text(yaml.safe_dump(config))

    def test_a_missing_resolver_settings_file_is_refused_before_resolving(self) -> None:
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config["build"]["options"]["variant-reference"] = str(self.fixture.root / "absent.gz")
        self.fixture.config_path.write_text(yaml.safe_dump(config))
        run = _run_cli(self.fixture)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("--variant-reference", run.stderr)
        self.assertNotIn("Traceback", run.stderr)

    def test_high_projected_overflow_blocks_candidate_publication(self) -> None:
        self._declare_variant_reference()
        outcomes = json.loads(self.fixture.outcomes_path.read_text())
        outcomes["GCST90000001"]["variant_reference_rows_matched"] = 0
        self.fixture.outcomes_path.write_text(json.dumps(outcomes))
        run = _run_cli(self.fixture)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("GCST90000001", run.stderr + run.stdout)
        self.assertIn("off-reference share", run.stderr + run.stdout)
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())

    def test_zero_build_eligible_rows_excludes_and_publication_succeeds(self) -> None:
        # OGS-00011's blocker: 476 successful records with zero build-eligible
        # rows refused the candidate through the overlap gate. They now carry
        # one audited exclusion reason and the overlap gate never sees them.
        self._declare_variant_reference()
        self._set_outcome("GCST90000001", build_eligible_rows=0)
        self._set_outcome("GCST90000001", build_eligible_rows_on_variant_reference=0)
        self._set_outcome("GCST90000001", build_eligible_rows_off_variant_reference=0)
        run = _run_cli(self.fixture)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        bundle = self.fixture.registry_root / STORE_ID
        analyses = {row["analysis_id"]: row for row in _read_tsv(bundle / "analyses.tsv")}
        self.assertEqual(analyses["GCST90000001"]["exclude_from_build"], "true")
        self.assertIn(
            "excluded: no_build_eligible_rows:",
            analyses["GCST90000001"]["inclusion_reason"],
        )
        exclusions = {
            row["analysis_id"]: row for row in _read_tsv(bundle / "sidecars/exclusions.tsv")
        }
        row = exclusions["GCST90000001"]
        self.assertEqual(row["reason"], "no_build_eligible_rows")
        self.assertEqual(row["category"], "effect_scale")
        self.assertEqual(row["resolver_status"], "success")
        self.assertEqual(row["exclude_from_build"], "true")
        self.assertIn("finite effect and positive standard error", row["detail"])
        self.assertIn("canonical_rows_retained=2000", row["detail"])
        overlap = {
            row["analysis_id"]: row
            for row in _read_tsv(bundle / "sidecars/reference_overlap.tsv")
        }
        self.assertEqual(overlap["GCST90000001"]["build_eligible_rows"], "0")
        self.assertEqual(overlap["GCST90000001"]["exclude_from_build"], "true")
        validation = yaml.safe_load((bundle / "validation.yaml").read_text())
        self.assertEqual(validation["checks"]["reference_overlap"], "passed")
        self.assertEqual(validation["errors"], [])

    def test_bundle_overlap_sidecar_and_gate_name_worst_contributor(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        first = _synthetic_inventory_row()
        second = replace(first, analysis_id="GCST90000002", data_file="/mirror/second.gz")
        records = []
        for row, matches in ((first, 100), (second, 0)):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            record["diagnostics"].update(
                ancestry_rows_read=100, ancestry_reference_rows_matched=90,
                variant_reference_rows_matched=matches,
            )
            records.append(record)
        outcomes = apply_release_policy(
            [first, second], config,
            {row.analysis_id: _synthetic_candidate_metadata() for row in (first, second)},
            {record["analysis_id"]: record for record in records},
        )
        tables = build_candidate_tables(
            inventory_rows=[first, second], outcomes=outcomes, config=config, index_summary={}
        )
        overlap = list(csv.DictReader(io.StringIO(tables.reference_overlap_tsv), delimiter="\t"))
        self.assertEqual([row["variant_reference_rate"] for row in overlap], ["1", "0"])
        self.assertEqual([row["rows_scanned"] for row in overlap], ["100", "100"])
        validation = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="now", validator_name="test"
        )
        self.assertEqual(validation["checks"]["reference_overlap"], "failed")
        self.assertEqual(validation["reference_overlap"]["projected_off_reference_share"], 0.5)
        self.assertEqual(validation["reference_overlap"]["low_overlap_analyses"], ["GCST90000002"])
        self.assertIn("GCST90000002", " ".join(validation["errors"]))

    def test_overlap_gate_prefers_post_info_build_eligible_counts(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        first = _synthetic_inventory_row()
        second = replace(first, analysis_id="GCST90000002", data_file="/mirror/second.gz")
        records = []
        # The pre-INFO legacy counts say every row matched, but the post-INFO
        # eligible rows do not: 40/50 for the first and 0/50 for the second.
        for row, eligible, on, legacy_matched in ((first, 50, 40, 100), (second, 50, 0, 100)):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            record["diagnostics"].update(
                ancestry_rows_read=100, ancestry_reference_rows_matched=90,
                variant_reference_rows_matched=legacy_matched,
                build_eligible_rows=eligible,
                build_eligible_rows_on_variant_reference=on,
                build_eligible_rows_off_variant_reference=eligible - on,
                info_score_state="filtered", info_rows_usable=eligible,
            )
            records.append(record)
        outcomes = apply_release_policy(
            [first, second], config,
            {row.analysis_id: _synthetic_candidate_metadata() for row in (first, second)},
            {record["analysis_id"]: record for record in records},
        )
        tables = build_candidate_tables(
            inventory_rows=[first, second], outcomes=outcomes, config=config, index_summary={}
        )
        overlap = list(csv.DictReader(io.StringIO(tables.reference_overlap_tsv), delimiter="\t"))
        # The legacy columns are left describing the pre-INFO rows, unchanged.
        self.assertEqual([row["variant_reference_rate"] for row in overlap], ["1", "1"])
        self.assertEqual([row["rows_scanned"] for row in overlap], ["100", "100"])
        self.assertEqual(
            [row["build_eligible_rows"] for row in overlap], ["50", "50"]
        )
        self.assertEqual(
            [row["build_eligible_rows_on_variant_reference"] for row in overlap], ["40", "0"]
        )
        self.assertEqual([row["build_eligible_rate"] for row in overlap], ["0.8", "0"])
        validation = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="now", validator_name="test"
        )
        summary = validation["reference_overlap"]
        self.assertEqual(summary["projected_off_reference_basis"], "build_eligible_rows")
        self.assertEqual(summary["projected_off_reference_share"], 0.6)
        self.assertEqual(summary["low_overlap_analyses"], ["GCST90000002"])
        self.assertEqual(validation["checks"]["reference_overlap"], "failed")

    def test_overlap_gate_falls_back_to_legacy_counts_without_diagnostics(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        row = _synthetic_inventory_row()
        record = _synthetic_resolver_record()
        record["analysis_id"] = row.analysis_id
        record["diagnostics"].update(
            rows_read=100, ancestry_rows_read=100, ancestry_reference_rows_matched=100,
            variant_reference_rows_matched=80,
        )
        outcomes = apply_release_policy(
            [row], config, {row.analysis_id: _synthetic_candidate_metadata()},
            {row.analysis_id: record},
        )
        tables = build_candidate_tables(
            inventory_rows=[row], outcomes=outcomes, config=config, index_summary={}
        )
        overlap = list(csv.DictReader(io.StringIO(tables.reference_overlap_tsv), delimiter="\t"))
        self.assertEqual([r["build_eligible_rows"] for r in overlap], [""])
        validation = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="now", validator_name="test"
        )
        summary = validation["reference_overlap"]
        self.assertEqual(summary["projected_off_reference_basis"], "legacy_rows_read")
        self.assertEqual(summary["projected_off_reference_share"], 0.2)

    def test_listed_effect_placeholder_analysis_is_excluded_with_its_reason(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(
            config,
            effect_placeholder_exclusions={"GCST90000002": "5 of 100 rows are placeholders"},
        )
        first = _synthetic_inventory_row()
        second = replace(first, analysis_id="GCST90000002", data_file="/mirror/second.gz")
        records = {}
        for row in (first, second):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            records[row.analysis_id] = record
        outcomes = apply_release_policy(
            [first, second], config,
            {row.analysis_id: _synthetic_candidate_metadata() for row in (first, second)},
            records,
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertTrue(by_id["GCST90000001"].included)
        self.assertFalse(by_id["GCST90000002"].included)
        self.assertEqual(by_id["GCST90000002"].exclusion_reason, "effect_placeholder_rows")
        self.assertEqual(
            by_id["GCST90000002"].exclusion_detail, "5 of 100 rows are placeholders"
        )
        tables = build_candidate_tables(
            inventory_rows=[first, second], outcomes=outcomes, config=config, index_summary={}
        )
        self.assertEqual(dict(tables.exclusion_counts), {"effect_placeholder_rows": 1})
        (excluded,) = csv.DictReader(io.StringIO(tables.exclusions_tsv), delimiter="\t")
        self.assertEqual(excluded["category"], "effect_scale")
        self.assertEqual(excluded["reason"], "effect_placeholder_rows")

    def test_non_positive_or_non_finite_sd_estimate_is_no_evidence(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        row = _synthetic_inventory_row()
        for sd in (0.0, -1.0, float("nan"), float("inf")):
            record = _synthetic_resolver_record()
            record["phenotype_sd"]["estimate"]["sd"] = sd
            (outcome,) = apply_release_policy(
                [row], config, {row.analysis_id: _synthetic_candidate_metadata()},
                {row.analysis_id: record},
            )
            self.assertFalse(outcome.included, sd)
            self.assertEqual(outcome.exclusion_reason, "sd_no_qualifying_evidence")
            self.assertIn("not a positive finite number", outcome.exclusion_detail)

    def test_committed_effect_placeholder_exclusions_are_readable(self) -> None:
        path = REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/effect-placeholder-exclusions.tsv"
        found = read_effect_placeholder_exclusions(path)
        self.assertEqual(len(found), 27)
        for analysis_id in ("GCST90454200", "GCST90454201", "GCST90565871", "GCST90565872"):
            self.assertIn("standard_error 0", found[analysis_id])
        self.assertEqual(read_effect_placeholder_exclusions(None), {})
        duplicate = self.fixture.root / "placeholder-duplicate.tsv"
        duplicate.write_text("analysis_id\treason\nA\tx\nA\ty\n", encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "duplicate analysis_id"):
            read_effect_placeholder_exclusions(duplicate)
        config = load_candidate_configuration(
            REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/config-full.yaml", REPO_ROOT
        )
        self.assertEqual(config.effect_placeholder_exclusions, found)

    # -- Indexed Variant Subset defaults (stores #210, epic #214) -------------

    def test_indexed_subsets_default_opt_out_and_malformed_errors(self) -> None:
        # Default: a config that says nothing emits the maintained hm3 entry.
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(len(config.indexed_subsets), 1)
        self.assertEqual(config.indexed_subsets[0]["name"], "hm3")
        self.assertEqual(config.indexed_subsets[0]["resource_id"], "hapmap3-hg38")
        self.assertIsNone(config.indexed_subsets_opt_out)
        build = candidate_workflow.render_build_yaml(STORE_ID, config)
        self.assertEqual(build["indexed_subsets"][0]["name"], "hm3")

        # A reviewed opt-out is an explicit empty list plus a written reason;
        # the build still declares the key, so the omission is reviewable.
        self._set_indexed_subsets([])
        self._set_indexed_subsets_opt_out("pilot predates the maintained HapMap3 list")
        opt_out = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(tuple(opt_out.indexed_subsets), ())
        self.assertEqual(
            opt_out.indexed_subsets_opt_out,
            "pilot predates the maintained HapMap3 list",
        )
        self.assertEqual(
            candidate_workflow.render_build_yaml(STORE_ID, opt_out)[
                "indexed_subsets"
            ],
            [],
        )

        # Empty without a reason is a generator error, not a silent omission.
        self._set_indexed_subsets_opt_out(None)
        with self.assertRaisesRegex(ValueError, "record why"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

        # A null value is malformed, matching plan()/bundle.check().
        self._set_indexed_subsets(None)
        with self.assertRaisesRegex(ValueError, "not null"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

        # A declaration missing a required field, or with a bad checksum, errors.
        self._set_indexed_subsets([{"name": "hm3"}])
        with self.assertRaisesRegex(ValueError, "requires a non-empty string"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self._set_indexed_subsets([{
            "name": "hm3",
            "resource_id": "hapmap3-hg38",
            "variant_list": "/list.txt",
            "reference_assembly": "GRCh38",
            "checksum": "not-a-sha256",
        }])
        with self.assertRaisesRegex(ValueError, "64-character hex sha256"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

        # A Ragged release may never declare one.
        self._set_indexed_subsets([{
            "name": "hm3",
            "resource_id": "hapmap3-hg38",
            "variant_list": "/list.txt",
            "reference_assembly": "GRCh38",
        }])
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["build"]["layout"] = "ragged"
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        with self.assertRaisesRegex(ValueError, "Ragged"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def test_indexed_subsets_reviewed_opt_out_is_recorded_in_release_notes(self) -> None:
        self._set_indexed_subsets([])
        self._set_indexed_subsets_opt_out("pilot predates the maintained HapMap3 list")
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        bundle = self.fixture.registry_root / STORE_ID
        build = yaml.safe_load((bundle / "build.yaml").read_text())
        self.assertEqual(build["indexed_subsets"], [])
        release = yaml.safe_load((bundle / "release.yaml").read_text())
        self.assertIn("Indexed Variant Subsets (#210)", release["notes"])
        self.assertIn("intentionally omitted", release["notes"])
        self.assertIn("pilot predates the maintained HapMap3 list", release["notes"])

    # -- Store composition / MAF-filtered admission (issue #203) --------------

    def _maf_filtered_fixture(self):
        """A synthetic pair: one MAF-filtered Analysis, one with no applied floor."""
        filtered = _synthetic_inventory_row()
        unfiltered = replace(
            filtered, analysis_id="GCST90000002", data_file="/mirror/second.gz"
        )
        filtered_record = _synthetic_resolver_record()
        filtered_record["analysis_id"] = filtered.analysis_id
        filtered_record["diagnostics"].update(
            maf_state="filtered",
            build_eligible_rows=100,
            build_eligible_rows_off_variant_reference=40,
        )
        filtered_record["fingerprints"] = {
            "resolution_config": {"maf_threshold": 0.005}
        }
        unfiltered_record = _synthetic_resolver_record()
        unfiltered_record["analysis_id"] = unfiltered.analysis_id
        unfiltered_record["diagnostics"].update(
            maf_state="unavailable",
            build_eligible_rows=200,
            build_eligible_rows_off_variant_reference=50,
        )
        declarations = [
            ResolverRow(
                filtered.analysis_id, "/mirror/GCST90000001.h.tsv.gz",
                "opengwasdb.gwas-ssf", "sd", "estimated_from_source_maf", "5000",
                "a" * 64, "sha256", "100", maf_threshold="0.005",
            ),
            ResolverRow(
                unfiltered.analysis_id, "/mirror/second.gz", "opengwasdb.gwas-ssf",
                "sd", "estimated_from_source_maf", "5000", "b" * 64, "sha256",
                "100", maf_threshold="NaN",
            ),
        ]
        return filtered, unfiltered, [filtered_record, unfiltered_record], declarations

    def _store_composition_outcomes(self, config: object, records: list[dict]):
        filtered = _synthetic_inventory_row()
        unfiltered = replace(
            filtered, analysis_id="GCST90000002", data_file="/mirror/second.gz"
        )
        return apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_candidate_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            self._maf_filtered_fixture()[3],
        )

    def test_store_composition_excludes_unfiltered_and_keeps_maf_filtered(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_candidate_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations,
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertTrue(by_id["GCST90000001"].included)
        self.assertEqual(by_id["GCST90000001"].maf_threshold, "0.005")
        routed = by_id["GCST90000002"]
        self.assertFalse(routed.included)
        self.assertEqual(routed.exclusion_reason, "not_maf_filtered")
        # The last gate keeps the other decided fields: the reason is only Store
        # composition, not an ancestry or effect-scale failure.
        self.assertEqual(routed.assigned_ancestry, "EUR")
        self.assertEqual(routed.original_sd, "1")
        self.assertEqual(
            routed.exclusion_detail,
            "no MAF floor applied (requested maf_threshold=NaN, resolver "
            "maf_state=unavailable, genotyping_technology=unrecorded); 50 of 200 "
            "build-eligible rows are off the variant reference (#203)",
        )
        tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=config,
            index_summary={},
        )
        self.assertEqual(dict(tables.exclusion_counts), {"not_maf_filtered": 1})
        (excluded,) = csv.DictReader(io.StringIO(tables.exclusions_tsv), delimiter="\t")
        self.assertEqual(excluded["category"], "store_composition")
        self.assertEqual(excluded["reason"], "not_maf_filtered")

    def test_store_composition_keeps_a_stronger_existing_exclusion(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        # The unfiltered Analysis already fails the target-ancestry gate.
        records[1]["ancestry"]["assigned_ancestry"] = "EAS"
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_candidate_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations,
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertEqual(by_id["GCST90000002"].exclusion_reason, "ancestry_not_eur")

    def test_store_composition_refuses_to_run_without_manifest_rows(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, _ = self._maf_filtered_fixture()
        # Without the manifest no Analysis can show an applied floor, so the rule
        # would exclude every one of them; that must fail, not empty the Store.
        with self.assertRaisesRegex(CandidateError, "resolver manifest rows"):
            apply_release_policy(
                [filtered, unfiltered],
                config,
                {
                    row.analysis_id: _synthetic_candidate_metadata()
                    for row in (filtered, unfiltered)
                },
                {record["analysis_id"]: record for record in records},
            )

    def test_store_composition_detail_names_a_missing_declaration(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_candidate_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations[:1],
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertEqual(by_id["GCST90000002"].exclusion_reason, "not_maf_filtered")
        # An absent manifest row is not the same fact as a declared NaN request.
        self.assertIn(
            "requested maf_threshold=undeclared", by_id["GCST90000002"].exclusion_detail
        )

    def test_store_composition_off_warns_about_unfiltered_inclusion(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            maf_threshold="0.005",
        )
        self.assertFalse(config.require_maf_filtered)
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_candidate_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations,
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertTrue(by_id["GCST90000002"].included)
        self.assertEqual(by_id["GCST90000002"].maf_threshold, "NaN")
        tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=config,
            index_summary={},
        )
        self.assertIn(
            "1 included Analysis/Analyses had no MAF floor applied, so their "
            "off-reference rows enter the shared variant axis unfiltered (#203); "
            "see store_composition in validation.yaml",
            tables.warnings,
        )

    def test_store_composition_summary_values_and_missing_sums(self) -> None:
        base = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        axis = replace(
            base, build_options={**base.build_options, "variant-reference": "/axis.gz"}
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = self._store_composition_outcomes(axis, records)
        tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=axis,
            index_summary={},
        )
        self.assertEqual(
            tables.store_composition,
            {
                "require_maf_filtered": True,
                "maf_floor": "0.005",
                "unfiltered_included": 0,
                "unfiltered_included_off_reference_rows": 0,
                "unfiltered_routed": 1,
                "unfiltered_routed_off_reference_rows": 50,
            },
        )
        # No declared variant-reference: absence is never summed as zero.
        no_axis = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=base,
            index_summary={},
        )
        self.assertIsNone(
            no_axis.store_composition["unfiltered_routed_off_reference_rows"]
        )
        # A counted Analysis without a valid off-reference count makes the sum
        # unknown, not zero.
        del records[1]["diagnostics"]["build_eligible_rows_off_variant_reference"]
        missing = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=self._store_composition_outcomes(axis, records),
            config=axis,
            index_summary={},
        )
        self.assertIsNone(
            missing.store_composition["unfiltered_routed_off_reference_rows"]
        )
        # Rule off: the unfiltered Analysis is included and its off-reference
        # rows are counted, while nothing is routed.
        off_axis = replace(axis, require_maf_filtered=False)
        included_tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=self._store_composition_outcomes(off_axis, records),
            config=off_axis,
            index_summary={},
        )
        self.assertEqual(included_tables.store_composition["unfiltered_included"], 1)
        self.assertEqual(included_tables.store_composition["unfiltered_routed"], 0)

    def test_store_composition_configuration_parsing(self) -> None:
        self.assertFalse(
            load_candidate_configuration(
                self.fixture.config_path, REPO_ROOT
            ).require_maf_filtered
        )
        self._set_store_composition({"require_maf_filtered": True})
        # The rule needs a positive default floor or no Analysis can be filtered.
        with self.assertRaisesRegex(ValueError, "positive.*maf_threshold"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self._set_maf_threshold(0.005)
        self.assertTrue(
            load_candidate_configuration(
                self.fixture.config_path, REPO_ROOT
            ).require_maf_filtered
        )
        self._set_maf_threshold(0)
        with self.assertRaisesRegex(ValueError, "positive.*maf_threshold"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        for invalid, message in (
            (["require_maf_filtered"], "store_composition must be a mapping"),
            ({"unknown": True}, "unknown key"),
            ({"require_maf_filtered": "yes"}, "must be a boolean"),
            ({"require_maf_filtered": 1}, "must be a boolean"),
        ):
            with self.subTest(invalid=invalid):
                self._set_store_composition(invalid)
                with self.assertRaisesRegex(ValueError, message):
                    load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def test_store_composition_rule_does_not_change_the_resolution_contract(self) -> None:
        self._set_maf_threshold(0.005)
        off = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        on = replace(off, require_maf_filtered=True)
        self.assertEqual(
            compute_resolution_contract(off), compute_resolution_contract(on)
        )

    def test_store_composition_documents_carry_the_rule(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_candidate_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations,
        )
        tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=config,
            index_summary={},
        )
        validation = render_validation_yaml(
            tables=tables,
            index_summary={},
            validated_at="now",
            validator_name="test",
        )
        self.assertEqual(validation["store_composition"], dict(tables.store_composition))
        release = render_release_yaml(
            store_id=STORE_ID,
            config=config,
            tables=tables,
            created_at="2026-01-01T00:00:00Z",
            inventory_sha256="a" * 64,
            preflight_report=Path("preflight.json"),
            index_summary={},
            generator_version="test",
        )
        self.assertIn(
            "Store composition (#203): this shared Store admits only Analyses whose "
            "rows were MAF-filtered; an included Analysis with no applied MAF floor "
            "is excluded as not_maf_filtered.",
            release["notes"],
        )

    def test_committed_store_composition_requires_maf_filtering(self) -> None:
        config = load_candidate_configuration(
            REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/config-full.yaml",
            REPO_ROOT,
        )
        self.assertTrue(config.require_maf_filtered)

    def test_zero_build_eligible_rows_is_excluded_and_not_missing(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        first = _synthetic_inventory_row()
        second = replace(first, analysis_id="GCST90000002", data_file="/mirror/second.gz")
        records = []
        # A successful record whose own build-eligible tally is an explicit
        # integer zero contributes no association at all.
        for row, eligible, on in ((first, 50, 40), (second, 0, 0)):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            record["diagnostics"].update(
                ancestry_rows_read=100, ancestry_reference_rows_matched=90,
                variant_reference_rows_matched=100,
                canonical_rows_retained=100,
                build_eligible_rows=eligible,
                build_eligible_rows_on_variant_reference=on,
                build_eligible_rows_off_variant_reference=eligible - on,
            )
            records.append(record)
        outcomes = apply_release_policy(
            [first, second], config,
            {row.analysis_id: _synthetic_candidate_metadata() for row in (first, second)},
            {record["analysis_id"]: record for record in records},
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertTrue(by_id["GCST90000001"].included)
        self.assertFalse(by_id["GCST90000002"].included)
        self.assertEqual(
            by_id["GCST90000002"].exclusion_reason, "no_build_eligible_rows"
        )
        self.assertIn(
            "no row with a finite effect and positive standard error",
            by_id["GCST90000002"].exclusion_detail,
        )
        self.assertIn("rows_read=100", by_id["GCST90000002"].exclusion_detail)
        self.assertIn(
            "canonical_rows_retained=100", by_id["GCST90000002"].exclusion_detail
        )
        tables = build_candidate_tables(
            inventory_rows=[first, second], outcomes=outcomes, config=config, index_summary={}
        )
        # The excluded Analysis is not an included Analysis with a missing
        # measurement: the zero-eligible count is reported, never a failure.
        self.assertEqual(tables.reference_overlap_errors, ())
        self.assertEqual(dict(tables.exclusion_counts), {"no_build_eligible_rows": 1})
        overlap = list(csv.DictReader(io.StringIO(tables.reference_overlap_tsv), delimiter="\t"))
        self.assertEqual([row["build_eligible_rows"] for row in overlap], ["50", "0"])
        self.assertEqual([row["exclude_from_build"] for row in overlap], ["", "true"])
        validation = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="now", validator_name="test"
        )
        self.assertEqual(validation["checks"]["reference_overlap"], "passed")
        self.assertEqual(validation["errors"], [])

    def test_invalid_build_eligible_counts_still_fail_the_overlap_gate(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        row = _synthetic_inventory_row()
        invalid = {
            "absent": {},
            "non-integer": {"build_eligible_rows": "50"},
            "negative": {"build_eligible_rows": -1},
            "inconsistent-partition": {
                "build_eligible_rows": 50,
                "build_eligible_rows_on_variant_reference": 20,
                "build_eligible_rows_off_variant_reference": 20,
            },
            "out-of-range-partition": {
                "build_eligible_rows": 50,
                "build_eligible_rows_on_variant_reference": 60,
                "build_eligible_rows_off_variant_reference": -10,
            },
        }
        for name, diagnostics in invalid.items():
            with self.subTest(case=name):
                record = _synthetic_resolver_record()
                record["analysis_id"] = row.analysis_id
                record["diagnostics"].update(
                    ancestry_rows_read=100, ancestry_reference_rows_matched=100,
                )
                record["diagnostics"].update(diagnostics)
                outcomes = apply_release_policy(
                    [row], config, {row.analysis_id: _synthetic_candidate_metadata()},
                    {row.analysis_id: record},
                )
                # Only an explicit integer zero is a policy exclusion; every other
                # unusable count stays an included Analysis whose measurement is
                # missing, exactly as before #176.
                self.assertTrue(outcomes[0].included, outcomes[0].exclusion_reason)
                tables = build_candidate_tables(
                    inventory_rows=[row], outcomes=outcomes, config=config, index_summary={}
                )
                self.assertTrue(tables.reference_overlap_errors, name)
                self.assertIn(row.analysis_id, " ".join(tables.reference_overlap_errors))

    # -- EAF orientation vocabulary (issue #115 / #154) -----------------------

    def _orientation_outcome(
        self, *, eaf_orientation: str, gate_reason: str, assigned: str | None, r: float = 0.99
    ):
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        row = _synthetic_inventory_row()
        metadata = _synthetic_candidate_metadata()
        record = _synthetic_resolver_record(
            eaf_orientation=eaf_orientation,
            gate_reason=gate_reason,
            assigned_ancestry=assigned,
            eaf_orientation_r=r,
        )
        return apply_release_policy(
            [row], config, {row.analysis_id: metadata}, {row.analysis_id: record}
        )[0]

    def test_orientation_passed_eur_is_includable(self) -> None:
        outcome = self._orientation_outcome(
            eaf_orientation="passed", gate_reason="ok", assigned="EUR"
        )
        self.assertTrue(outcome.included, outcome.exclusion_reason)
        self.assertEqual(outcome.assigned_ancestry, "EUR")

    def test_orientation_failed_is_orientation_failure(self) -> None:
        # Real shape: a negative sign is also the assignment gate that names it.
        outcome = self._orientation_outcome(
            eaf_orientation="failed", gate_reason="eaf_orientation", assigned=None, r=-0.98
        )
        self.assertFalse(outcome.included)
        self.assertEqual(outcome.exclusion_reason, "orientation_failure")

    def test_orientation_failed_excludes_even_with_ok_gate(self) -> None:
        # A mildly negative correlation is `failed` without tripping the gate.
        outcome = self._orientation_outcome(
            eaf_orientation="failed", gate_reason="ok", assigned="EUR", r=-0.2
        )
        self.assertFalse(outcome.included)
        self.assertEqual(outcome.exclusion_reason, "orientation_failure")

    def test_orientation_unverified_follows_assignment_policy(self) -> None:
        # `unverified` is not a verdict: the ordinary policy decides the row.
        included = self._orientation_outcome(
            eaf_orientation="unverified", gate_reason="ok", assigned="EUR", r=float("nan")
        )
        self.assertTrue(included.included, included.exclusion_reason)
        unassigned = self._orientation_outcome(
            eaf_orientation="unverified", gate_reason="overlap", assigned=None, r=float("nan")
        )
        self.assertFalse(unassigned.included)
        self.assertEqual(unassigned.exclusion_reason, "ancestry_unassigned")

    def test_unknown_eaf_orientation_fails_loudly(self) -> None:
        # `ok` is a gate_reason, not an EafOrientationOutcome; it must not read as passed.
        with self.assertRaises(CandidateError):
            self._orientation_outcome(eaf_orientation="ok", gate_reason="ok", assigned="EUR")

    def test_validate_candidate_analyses_rejects_blank_included_required_value(self) -> None:
        header = "analysis_id\tstored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\toriginal_effect_scale\toriginal_sd_method\tancestry_assignment_method\n"
        blank = "GCST1\tsd\ttotal\tanalysis_level\t\tsd\testimated_from_source_maf\taf_assigned\n"
        self.assertTrue(validate_candidate_analyses(header + blank))

    # -- snakemake wiring ------------------------------------------------------

    @unittest.skipUnless(
        SNAKEMAKE, "snakemake is not on PATH; run under `pixi run --environment dev`"
    )
    def test_generate_smk_dry_run_wires_every_stage(self) -> None:
        snakefile = REPO_ROOT / "workflow/generate.smk"
        work_root = self.fixture.root / "smk-work"
        registry_root = self.fixture.root / "smk-stores"
        command = [
            str(SNAKEMAKE),
            "--snakefile",
            str(snakefile),
            "--dry-run",
            "--cores",
            "1",
            "--config",
            f"store_id={STORE_ID}",
            f"config={self.fixture.config_path}",
            f"snapshot_id={SNAPSHOT_ID}",
            f"work_root={work_root}",
            f"registry_root={registry_root}",
        ]
        result = subprocess.run(
            command, cwd=str(self.fixture.root), capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for rule in ("preflight", "resolver_manifest", "resolve", "verify", "finalise"):
            self.assertIn(rule, result.stdout)
        # The same DAG refuses to run without its required configuration.
        missing = subprocess.run(
            [str(SNAKEMAKE), "--snakefile", str(snakefile), "--dry-run", "--cores", "1"],
            cwd=str(self.fixture.root),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("store_id", missing.stderr + missing.stdout)


class SharedVocabularyTests(unittest.TestCase):
    """The workflow uses the contract's and Manifest Generation's vocabulary, not its own copy (#250)."""

    def test_super_population_codes_come_from_the_contract(self) -> None:
        self.assertIs(candidate_workflow.SUPERPOPULATIONS, bundle_module.SUPERPOPULATIONS)

    def test_info_score_vocabulary_comes_from_the_generation_rules(self) -> None:
        from manifest_generation import generation_rules

        self.assertIs(candidate_workflow.INFO_SCORE_KINDS, generation_rules.INFO_SCORE_KINDS)


# ---------------------------------------------------------------------------
# Resolution Records kept once per Source Collection, reused by every Selection
# (#284, ADR 0036). ``FAKE_RESOLVER_LOG`` records which Analyses a run actually
# resolved, as opposed to resumed.
# ---------------------------------------------------------------------------

COLLECTION_ID = "gwas-catalog-ssf"
TRIAL_STORE_ID = "OGS-99002"
READY_IDS = [row[0] for row in READY_ANALYSES]
#: Every outcome in the trial Selection is a success, so nothing in it is
#: re-run as a controlled failure would be.
TRIAL_IDS = ["GCST90000001", "GCST90000002", "GCST90000004"]


class SharedResolutionRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="resolution-records-")
        self.fixture: Fixture = FixtureBuilder(Path(self._tmp.name)).build()
        self.area = ResolutionArea.for_collection(self.fixture.resolution_root, COLLECTION_ID)
        self._runs = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------------

    def _all_success_outcomes(self) -> Path:
        path = self.fixture.root / "all-success.json"
        path.write_text("{}", encoding="utf-8")
        return path

    def _trial_config(self, analysis_ids: list[str]) -> Path:
        """A second Selection over the same Source Collection: a Trial Store Release."""
        root = self.fixture.root
        keep = set(analysis_ids)
        acquisition = [
            row for row in _read_tsv(root / "acq" / "base.tsv") if row["analysis_id"] in keep
        ]
        passes = []
        for role, name in (("base", "trial-base.tsv"), ("retry_transient", "trial-retry.tsv")):
            path = root / "acq" / name
            _write_tsv(path, ACQUISITION_MANIFEST_COLUMNS, acquisition)
            passes.append(AcquisitionPass(role=role, path=path))
        candidates = _read_tsv(self.fixture.candidates_path)
        trial_candidates = root / "trial-candidates.tsv"
        _write_tsv(
            trial_candidates,
            list(candidates[0]),
            [row for row in candidates if row["STUDY.ACCESSION"] in keep],
        )
        snapshot = build_snapshot(
            snapshot_id="fixture-trial",
            source_collection_id=COLLECTION_ID,
            store_key=STORE_KEY,
            ancestry_group="European",
            manifests=passes,
            candidates=read_candidate_selection(trial_candidates, STORE_KEY),
            frozen_at="2026-09-21T00:00:00Z",
        )
        inventory = root / "inventory" / "fixture-trial.tsv"
        provenance = root / "inventory" / "fixture-trial.meta.yaml"
        write_snapshot(snapshot, inventory, provenance)

        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["label"] = "fixture-trial"
        document["source"]["candidates"] = str(trial_candidates)
        document["source"]["inventory"] = {
            "snapshot_id": "fixture-trial",
            "path": str(inventory),
            "provenance_path": str(provenance),
            "freeze_inputs": [
                {"role": p.role, "path": str(p.path)} for p in passes
            ],
        }
        config_path = root / "config-trial.yaml"
        config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return config_path

    def _run(self, *args: str, **kwargs):
        """Run the entry point; return (result, the Analyses this run resolved)."""
        self._runs += 1
        log = self.fixture.root / f"resolved-{self._runs}.log"
        result = _run_cli(self.fixture, *args, resolved_log=log, **kwargs)
        resolved = log.read_text(encoding="utf-8").split() if log.exists() else []
        return result, sorted(resolved)

    def _receipt(self, store_id: str = STORE_ID) -> dict:
        path = self.fixture.work_root / store_id / "resolver" / RECEIPT_FILENAME
        return json.loads(path.read_text(encoding="utf-8"))

    def _set_gate(self, name: str, value: float) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["ancestry_assignment"]["gates"][name] = value
        self.fixture.config_path.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

    def _assert_names_kept_records(self, receipt: dict, expected_ids: list[str]) -> None:
        self.assertEqual(receipt["analysis_ids"], expected_ids)
        self.assertEqual(list(receipt["records"]), expected_ids)
        self.assertEqual(receipt["resolution_area"], str(self.area.root))
        for analysis_id, entry in receipt["records"].items():
            path = self.area.root / entry["record"]
            self.assertTrue(path.is_file(), path)
            self.assertEqual(path, self.area.record_path(
                KeptRecord(analysis_id, entry["fingerprint_digest"], entry["record_sha256"])
            ))
            data = path.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["record_sha256"])
            record = json.loads(data)
            self.assertEqual(record["analysis_id"], analysis_id)
            self.assertEqual(
                record["fingerprints"]["fingerprint_digest"], entry["fingerprint_digest"]
            )

    def _assert_emit_fails(self, expected: str) -> None:
        before = _candidate_tables_bytes(self.fixture.registry_root)
        result, _ = self._run("--stage", "emit")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)
        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)

    # -- 1. two Selections over one collection ---------------------------------

    def test_trial_then_full_release_resolve_each_shared_analysis_once(self) -> None:
        trial, resolved = self._run(
            store_id=TRIAL_STORE_ID, config_path=self._trial_config(TRIAL_IDS)
        )
        self.assertEqual(trial.returncode, 0, trial.stderr)
        self.assertEqual(resolved, TRIAL_IDS)

        full, resolved = self._run()
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(resolved, sorted(set(READY_IDS) - set(TRIAL_IDS)))

        trial_receipt = self._receipt(TRIAL_STORE_ID)
        full_receipt = self._receipt()
        self._assert_names_kept_records(trial_receipt, TRIAL_IDS)
        self._assert_names_kept_records(full_receipt, READY_IDS)
        # The shared Analyses' records are the same records, not re-measurements.
        for analysis_id in TRIAL_IDS:
            self.assertEqual(
                trial_receipt["records"][analysis_id], full_receipt["records"][analysis_id]
            )

        # Both candidates still verify and re-emit from their own receipts.
        for store_id, config in (
            (TRIAL_STORE_ID, self.fixture.root / "config-trial.yaml"),
            (STORE_ID, self.fixture.config_path),
        ):
            again, resolved = self._run("--stage", "emit", store_id=store_id, config_path=config)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(resolved, [])

    def test_a_selection_under_its_own_settings_is_never_re_resolved(self) -> None:
        # Trial under settings A, the full release under settings B, then the
        # trial again under A: the B run must not evict the trial's records.
        outcomes = self._all_success_outcomes()
        trial_config = self._trial_config(TRIAL_IDS)
        trial, resolved = self._run(
            store_id=TRIAL_STORE_ID, config_path=trial_config, outcomes=outcomes
        )
        self.assertEqual(trial.returncode, 0, trial.stderr)
        self.assertEqual(resolved, TRIAL_IDS)
        first = self._receipt(TRIAL_STORE_ID)

        self._set_gate("tau", 0.6)
        full, resolved = self._run(outcomes=outcomes)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(resolved, READY_IDS)
        self.assertNotEqual(
            self._receipt()["resolver"]["settings_digest"], first["resolver"]["settings_digest"]
        )

        again, resolved = self._run(
            store_id=TRIAL_STORE_ID, config_path=trial_config, outcomes=outcomes
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(resolved, [])
        # The same records, byte for byte: the fake's metrics differ on every
        # resolution, so a re-resolution would have changed every checksum.
        self.assertEqual(self._receipt(TRIAL_STORE_ID)["records"], first["records"])

    def test_re_resolving_under_one_fingerprint_changes_the_record_checksum(self) -> None:
        # The premise of the test above: a record resolved again is a new record.
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        first = self._receipt()["records"]
        shutil.rmtree(self.area.root / "checkpoint")
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, READY_IDS)
        for analysis_id, entry in self._receipt()["records"].items():
            self.assertEqual(entry["fingerprint_digest"], first[analysis_id]["fingerprint_digest"])
            self.assertNotEqual(entry["record_sha256"], first[analysis_id]["record_sha256"])

    def test_the_work_root_holds_no_records(self) -> None:
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        self.assertFalse((run_root / "records").exists())
        self.assertEqual(
            sorted(p.name for p in run_root.iterdir()),
            sorted(["analyses.tsv", "index.json", "resolve.log", RECEIPT_FILENAME]),
        )
        # The resolver's own records directory is the collection's checkpoint for
        # these resolver settings.
        receipt = self._receipt()
        checkpoint = self.area.checkpoint_dir(receipt["resolver"]["settings_digest"])
        self.assertEqual(checkpoint.parent, self.area.root / "checkpoint")
        self.assertIn(str(checkpoint), receipt["resolver"]["argv"])

    # -- 2. reuse and invalidation ---------------------------------------------

    def test_an_unchanged_run_re_resolves_nothing(self) -> None:
        outcomes = self._all_success_outcomes()
        first, resolved = self._run(outcomes=outcomes)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(resolved, READY_IDS)
        receipt = self._receipt()

        second, resolved = self._run(outcomes=outcomes)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(resolved, [])
        self.assertEqual(self._receipt()["records"], receipt["records"])

    def test_a_changed_file_re_resolves_only_its_analysis(self) -> None:
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        before = self._receipt()["records"]

        source = self.fixture.root / "mirror" / "GCST90000003.h.tsv.gz"
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, ["GCST90000003"])
        after = self._receipt()["records"]
        self.assertNotEqual(
            after["GCST90000003"]["fingerprint_digest"],
            before["GCST90000003"]["fingerprint_digest"],
        )
        self.assertEqual(
            {k: v for k, v in after.items() if k != "GCST90000003"},
            {k: v for k, v in before.items() if k != "GCST90000003"},
        )

    def test_a_changed_reference_re_resolves_and_keeps_the_old_records(self) -> None:
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        before = self._receipt()["records"]

        reference = self.fixture.root / "reference" / "ref_freqs.tsv.gz"
        reference.write_bytes(reference.read_bytes() + b"# a new reference build\n")
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, READY_IDS)
        after = self._receipt()["records"]
        for analysis_id in READY_IDS:
            self.assertNotEqual(
                after[analysis_id]["fingerprint_digest"], before[analysis_id]["fingerprint_digest"]
            )
            # A record is kept, never overwritten: the old receipt's still exist.
            self.assertTrue((self.area.root / before[analysis_id]["record"]).is_file())

    def test_a_changed_setting_re_resolves_every_analysis(self) -> None:
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        self._set_gate("tau", 0.6)
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, READY_IDS)
        # ... and the run after it, unchanged again, re-resolves nothing.
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, [])

    # -- 3. a receipt names exactly its records, and verification holds --------

    def test_a_missing_record_fails_verification(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        entry = self._receipt()["records"]["GCST90000002"]
        (self.area.root / entry["record"]).unlink()
        self._assert_emit_fails("missing")

    def test_a_stale_record_fails_verification(self) -> None:
        # The source changed after its record was kept: the record's fingerprint
        # no longer matches the file, so the candidate cannot be emitted from it.
        self.assertEqual(self._run()[0].returncode, 0)
        source = self.fixture.root / "mirror" / "GCST90000001.h.tsv.gz"
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
        self._assert_emit_fails("re-run resolution")

    def test_an_edited_record_fails_verification_by_its_checksum(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        path = self.area.root / self._receipt()["records"]["GCST90000001"]["record"]
        record = json.loads(path.read_text(encoding="utf-8"))
        record["fingerprints"]["resolution_config"]["gates"]["tau"] = 0.99
        record["fingerprints"]["fingerprint_digest"] = _fingerprint_digest(record["fingerprints"])
        path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        self._assert_emit_fails("record_sha256")

    def test_a_receipt_naming_a_record_outside_its_selection_fails(self) -> None:
        trial = self._run(store_id=TRIAL_STORE_ID, config_path=self._trial_config(TRIAL_IDS))[0]
        self.assertEqual(trial.returncode, 0, trial.stderr)
        self.assertEqual(self._run()[0].returncode, 0)
        full = self._receipt()["records"]

        receipt_path = self.fixture.work_root / TRIAL_STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["records"]["GCST90000003"] = full["GCST90000003"]
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        result, _ = self._run(
            "--stage", "emit",
            store_id=TRIAL_STORE_ID,
            config_path=self.fixture.root / "config-trial.yaml",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside", result.stderr)

    def _edit_receipt(self, edit) -> None:
        path = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt = json.loads(path.read_text(encoding="utf-8"))
        edit(receipt)
        path.write_text(json.dumps(receipt), encoding="utf-8")

    def test_a_receipt_without_a_resolution_area_is_refused(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        self._edit_receipt(lambda r: r.__setitem__("resolution_area", None))
        self._assert_emit_fails("resolution_area")
        self._edit_receipt(lambda r: r.pop("resolution_area"))
        self._assert_emit_fails("resolution_area")

    def test_a_receipt_with_an_unknown_key_is_refused(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        self._edit_receipt(lambda r: r.__setitem__("note", "hand-added"))
        self._assert_emit_fails("unknown key")

    def test_a_record_entry_with_an_unknown_key_is_refused(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        self._edit_receipt(
            lambda r: r["records"]["GCST90000001"].__setitem__("reused", True)
        )
        self._assert_emit_fails("GCST90000001")

    def test_a_historical_receipt_is_refused_by_name(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        receipt_path = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["receipt_schema_version"] = 1
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self._assert_emit_fails("historical")

    # -- 5. concurrency --------------------------------------------------------

    def test_a_second_run_over_a_locked_collection_area_is_refused(self) -> None:
        with self.area.exclusive("a concurrent test run"):
            result, resolved = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked", result.stderr)
        self.assertIn("a concurrent test run", result.stderr)
        self.assertEqual(resolved, [])
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())
        self.assertEqual(list((self.area.root / "checkpoint").rglob("*.json")), [])
        # Once the lock is released the same run proceeds.
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_killed_run_leaves_the_lock_with_its_running_resolver(self) -> None:
        pause = self.fixture.root / "resolver-paused"
        command, env = _cli_invocation(
            self.fixture, extra_env={"FAKE_RESOLVER_PAUSE_FILE": str(pause)}
        )
        parent = subprocess.Popen(
            command, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 60
            while not (pause.exists() and pause.read_text().strip()):
                self.assertLess(time.monotonic(), deadline, "the resolver never started")
                self.assertIsNone(parent.poll(), "the run exited before resolving")
                time.sleep(0.05)
            resolver_pid = int(pause.read_text())
            parent.kill()
            parent.wait()
            # The run is gone but its resolver is still writing: no second run.
            with self.assertRaises(ResolutionAreaLocked):
                with self.area.exclusive("a second run"):
                    pass
        finally:
            pause.unlink(missing_ok=True)
            if parent.poll() is None:
                parent.kill()
                parent.wait()
        deadline = time.monotonic() + 60
        while _pid_alive(resolver_pid):
            self.assertLess(time.monotonic(), deadline, "the resolver never finished")
            time.sleep(0.05)
        with self.area.exclusive("after the resolver finished"):
            pass

    def test_resume_is_refused_because_reuse_is_automatic(self) -> None:
        result, resolved = self._run("--resume")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reuse is automatic", result.stderr)
        self.assertEqual(resolved, [])

    def test_keeping_a_record_never_replaces_different_bytes(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        receipt = self._receipt()
        entry = receipt["records"]["GCST90000001"]
        kept = self.area.root / entry["record"]
        kept.write_bytes(kept.read_bytes() + b" ")
        checkpoint = self.area.checkpoint_dir(receipt["resolver"]["settings_digest"])
        with self.assertRaisesRegex(ResolutionAreaError, "differ"):
            self.area.keep(checkpoint, "GCST90000001")

    def test_verifying_a_resolved_candidate_needs_no_lock(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        with self.area.exclusive("another Selection resolving"):
            result, resolved = self._run("--stage", "emit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, [])

    def test_the_lock_is_exclusive(self) -> None:
        with self.area.exclusive("first"):
            with self.assertRaises(ResolutionAreaLocked) as raised:
                with self.area.exclusive("second"):
                    pass
            self.assertIn("first", str(raised.exception))
        with self.area.exclusive("third"):
            pass


#: The pinned resolver, installed in this environment; absent, the test skips.
REAL_RESOLVER = Path(sys.executable).resolve().parent / "opengwasdb"
_PANEL_GROUPS = ("North-West Europe", "West Africa", "East Asia")
_PANEL_SUPERPOPS = {"North-West Europe": "EUR", "West Africa": "AFR", "East Asia": "EAS"}
_PANEL_VARIANTS = 60


def _panel_frequency(index: int, group: int) -> float:
    return 0.1 + 0.8 * (((index * 37 + group * 11) % 97) / 97)


def _real_ssf_body(analysis_id: str) -> bytes:
    """A tiny GWAS-SSF file the real resolver reads, with EUR-like frequencies."""
    seed = int(analysis_id[-2:])
    lines = [
        "chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\t"
        "standard_error\teffect_allele_frequency\n"
    ]
    for index in range(_PANEL_VARIANTS):
        frequency = _panel_frequency(index, 0)
        beta = 0.01 * (((index + seed) % 7) - 3)
        lines.append(f"1\t{1000 + index}\tA\tC\t{beta:.4g}\t0.02\t{frequency:.6g}\n")
    return gzip.compress("".join(lines).encode("utf-8"), mtime=0)


@unittest.skipUnless(REAL_RESOLVER.is_file(), "the pinned opengwasdb resolver is not installed")
class RealResolverReuseTests(unittest.TestCase):
    """The pinned `opengwasdb resolve-analyses` reuses a Resolution Area's records (#284)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="real-resolver-")
        self.fixture = FixtureBuilder(Path(self._tmp.name), bodies=_real_ssf_body).build()
        reference = self.fixture.root / "reference"
        header = ["alid", "chromosome", "position", "effect_allele", "other_allele", "rsid"]
        rows = ["\t".join([*header, *_PANEL_GROUPS])]
        for index in range(_PANEL_VARIANTS):
            cells = [f"1:{1000 + index}:A:C", "1", str(1000 + index), "A", "C", f"rs{index}"]
            cells += [f"{_panel_frequency(index, g):.6g}" for g in range(len(_PANEL_GROUPS))]
            rows.append("\t".join(cells))
        (reference / "ref_freqs.tsv.gz").write_bytes(
            gzip.compress(("\n".join(rows) + "\n").encode("utf-8"), mtime=0)
        )
        (reference / "ancestry_groups.tsv").write_text(
            "group\tsuper_pop\n"
            + "".join(f"{g}\t{_PANEL_SUPERPOPS[g]}\n" for g in _PANEL_GROUPS),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_unchanged_second_run_resumes_every_record(self) -> None:
        receipts = []
        indexes = []
        for _ in range(2):
            result = _run_cli(self.fixture, "--stage", "resolve", resolver=REAL_RESOLVER)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            run_root = self.fixture.work_root / STORE_ID / "resolver"
            receipts.append(json.loads((run_root / RECEIPT_FILENAME).read_text()))
            indexes.append(json.loads((run_root / "index.json").read_text()))
        n = len(READY_ANALYSES)
        self.assertEqual(indexes[0]["n_total"], n)
        self.assertEqual(indexes[0]["n_success"], n, indexes[0])
        self.assertEqual(indexes[0]["n_resumed"], 0)
        self.assertEqual(indexes[1]["n_resumed"], n)
        self.assertEqual(receipts[1]["records"], receipts[0]["records"])


class ResolutionRootTests(unittest.TestCase):
    """Where the shared area lives: a deployment root, resolved like the Artifact Root.

    The flag, then ``OPENGWASDB_RESOLUTION_ROOT``, then ``ogstores.yaml``'s
    ``resolution_root``, then the built-in default. A release config cannot
    name it, so two configs over one collection cannot get two areas.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="resolution-root-")
        self.root = Path(self._tmp.name)
        self.fixture = FixtureBuilder(self.root / "fixture").build()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_precedence_flag_env_repo_config_default(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self.assertEqual(resolution_root(env={}, repo_root=repo), DEFAULT_RESOLUTION_ROOT)
        (repo / "ogstores.yaml").write_text("resolution_root: /from/config\n", encoding="utf-8")
        self.assertEqual(resolution_root(env={}, repo_root=repo), Path("/from/config"))
        env = {RESOLUTION_ROOT_ENV_VAR: "/from/env"}
        self.assertEqual(resolution_root(env=env, repo_root=repo), Path("/from/env"))
        self.assertEqual(
            resolution_root("/from/flag", env=env, repo_root=repo), Path("/from/flag")
        )

    def test_a_malformed_repo_config_value_is_refused(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        for text in ("resolution_root: ''\n", "resolution_root: relative/path\n", "- a list\n"):
            (repo / "ogstores.yaml").write_text(text, encoding="utf-8")
            with self.assertRaises(ResolutionAreaError):
                resolution_root(env={}, repo_root=repo)

    def test_the_tracked_repo_config_names_the_production_root(self) -> None:
        self.assertEqual(
            resolution_root(env={}, repo_root=REPO_ROOT), Path("/data/opengwasdb/work/resolution")
        )

    def test_the_pipeline_resolves_the_area_from_the_deployment_root(self) -> None:
        args = generate_candidate.parse_args(
            [STORE_ID, "--config", str(self.fixture.config_path),
             "--resolution-root", str(self.root / "flag")]
        )
        pipeline = generate_candidate.Pipeline(args, REPO_ROOT, "test")
        self.assertEqual(pipeline.resolution_area.root, self.root / "flag" / COLLECTION_ID)

    def test_a_release_config_cannot_name_the_resolution_root(self) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["output"]["resolution_root"] = str(self.root / "from-config")
        self.fixture.config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
        with self.assertRaisesRegex(PreflightConfigError, "deployment"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def test_a_collection_id_that_is_not_one_path_segment_is_refused(self) -> None:
        for bad in ("", "..", "a/b", ".hidden"):
            with self.assertRaises(ValueError):
                ResolutionArea.for_collection(self.root, bad)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is finished; only its reaping is left.
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split(")")[-1].split()[0] == "Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
