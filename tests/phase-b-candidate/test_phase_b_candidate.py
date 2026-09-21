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
   candidate.

Run from the repository root:
    python3 tests/phase-b-candidate/test_phase_b_candidate.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
for extra in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import resources.generators.lib.candidate_workflow as candidate_workflow  # noqa: E402
from ogstores import bundle as bundle_module  # noqa: E402
from ogstores.plan import plan  # noqa: E402
from resources.generators.lib.candidate_workflow import (  # noqa: E402
    EXCLUSION_REASONS,
    RECEIPT_FILENAME,
    RESOLVER_MANIFEST_COLUMNS,
    CandidateError,
    ResolverRow,
    account_records,
    apply_release_policy,
    build_candidate_tables,
    check_staged_candidate,
    derive_resolver_manifest,
    load_candidate_configuration,
    read_candidate_metadata,
    read_resolution_receipt,
    render_resolver_manifest,
    resolver_argv,
    validate_candidate_analyses,
    verify_records,
)
from resources.generators.lib.source_inventory import (  # noqa: E402
    ACQUISITION_MANIFEST_COLUMNS,
    build_snapshot,
    read_candidate_selection,
    read_inventory,
    write_snapshot,
)

CLI = REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/generate_candidate.py"
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
        "eaf_orientation": "eaf_orientation",
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


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FixtureBuilder:
    """Build one self-consistent fixture tree in a temporary directory."""

    def __init__(self, root: Path) -> None:
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
                body = (f"source-body-{content}\n" * (1 + ord(content[0]) % 3)).encode()
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
            base_manifest=base_manifest,
            retry_manifest=retry_manifest,
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
                    "freeze_inputs": {
                        "base_manifest": str(base_manifest),
                        "retry_manifest": str(retry_manifest),
                    },
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


def _two_resolver_rows() -> list[ResolverRow]:
    """Two minimal resolver rows for unit-level accounting tests."""
    return [
        ResolverRow("GCST1", "/tmp/a.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
                    "estimated_from_source_maf", "100", "a" * 64, "sha256", "10"),
        ResolverRow("GCST2", "/tmp/b.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
                    "estimated_from_source_maf", "200", "b" * 64, "sha256", "20"),
    ]


def _run_cli(
    fixture: Fixture,
    *args: str,
    cores: int = 1,
    resume: bool = False,
    outcomes: Path | None = None,
    fail_after: int | None = None,
    registry_root: Path | None = None,
    work_root: Path | None = None,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(CLI),
        STORE_ID,
        "--config",
        str(fixture.config_path),
        "--cores",
        str(cores),
        "--resolver",
        str(FAKE_RESOLVER),
        "--repo-root",
        str(REPO_ROOT),
        "--registry-root",
        str(registry_root or fixture.registry_root),
        "--work-root",
        str(work_root or fixture.work_root),
    ]
    if resume:
        command.append("--resume")
    command.extend(args)
    # The fake resolver is exec'd through its shebang, so put this interpreter's
    # directory first on PATH: it then runs under the same environment (and the
    # same installed opengwasdb) as the registry process.
    interpreter_dir = str(Path(sys.executable).resolve().parent)
    env = {
        **os.environ,
        "PATH": interpreter_dir + os.pathsep + os.environ.get("PATH", ""),
        "FAKE_RESOLVER_OUTCOMES": str(outcomes or fixture.outcomes_path),
    }
    if fail_after is not None:
        env["FAKE_RESOLVER_FAIL_AFTER"] = str(fail_after)
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
        for sidecar in ("source_readiness", "ancestry", "sd_estimation", "exclusions"):
            self.assertTrue((bundle_dir / "sidecars" / f"{sidecar}.tsv").is_file())

        analyses = _read_tsv(bundle_dir / "analyses.tsv")
        included = {row["analysis_id"] for row in analyses if row["exclude_from_build"] != "true"}
        excluded = {
            row["analysis_id"]: row for row in analyses if row["exclude_from_build"] == "true"
        }
        self.assertEqual(included, EXPECTED_INCLUDED)
        self.assertEqual(set(excluded), set(EXPECTED_EXCLUDED))

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
        self.assertIn("generate_candidate.py", commands)
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

        validation = yaml.safe_load((bundle_dir / "validation.yaml").read_text(encoding="utf-8"))
        self.assertIn(validation["status"], {"passed", "passed_with_warnings"})
        self.assertEqual(validation["checks"]["schema"], "passed")

        # The complete bundle passes the executable contract, and the active
        # (non-excluded) analyses pass the pinned OpenGWASDB schema.
        loaded = bundle_module.load(STORE_ID, registry_root=self.fixture.registry_root)
        self.assertEqual(list(bundle_module.check(loaded, registry_root=self.fixture.registry_root)), [])
        self.assertEqual(validate_candidate_analyses((bundle_dir / "analyses.tsv").read_text()), [])
        # The observed-only Hybrid recipe is plannable exactly as ADR 0023 requires.
        steps = plan(loaded)
        self.assertEqual(steps[0].name, "build")
        self.assertIn("build-hybrid", steps[0].argv)

    def test_worker_count_byte_equivalence(self) -> None:
        single_root = self.fixture.root / "stores-1"
        multi_root = self.fixture.root / "stores-4"
        single_root.mkdir()
        multi_root.mkdir()
        one = _run_cli(
            self.fixture, cores=1, registry_root=single_root, work_root=self.fixture.root / "work-1"
        )
        self.assertEqual(one.returncode, 0, one.stderr)
        four = _run_cli(
            self.fixture, cores=4, registry_root=multi_root, work_root=self.fixture.root / "work-4"
        )
        self.assertEqual(four.returncode, 0, four.stderr)
        self.assertEqual(_candidate_tables_bytes(single_root), _candidate_tables_bytes(multi_root))

    def test_resume_reuses_records_and_reproduces_bytes(self) -> None:
        first = _run_cli(self.fixture, resume=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = _candidate_tables_bytes(self.fixture.registry_root)

        second = _run_cli(self.fixture, resume=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("--resume", second.stdout)
        after = _candidate_tables_bytes(self.fixture.registry_root)
        self.assertEqual(before, after)

        index = json.loads(
            (self.fixture.work_root / STORE_ID / "resolver/records/index.json").read_text()
        )
        self.assertGreater(index["n_resumed"], 0)

    def test_kill_then_resume_matches_uninterrupted_run(self) -> None:
        ok_root = self.fixture.root / "stores-ok"
        ok_root.mkdir()
        ok = _run_cli(
            self.fixture, registry_root=ok_root, work_root=self.fixture.root / "work-ok"
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
            fail_after=3,
        )
        self.assertNotEqual(killed.returncode, 0)
        self.assertFalse((resume_root / STORE_ID).exists())

        resumed = _run_cli(
            self.fixture,
            resume=True,
            registry_root=resume_root,
            work_root=resume_work,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(_candidate_tables_bytes(resume_root), expected)
        index = json.loads(
            (resume_work / STORE_ID / "resolver/records/index.json").read_text()
        )
        self.assertEqual(index["n_resumed"], 3)

    def test_interrupted_run_preserves_prior_candidate(self) -> None:
        first = _run_cli(self.fixture)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = _candidate_tables_bytes(self.fixture.registry_root)
        release_before = (self.fixture.registry_root / STORE_ID / "release.yaml").read_bytes()

        interrupted = _run_cli(self.fixture, fail_after=2)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertIn("resolver exited", interrupted.stderr)

        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)
        self.assertEqual(
            (self.fixture.registry_root / STORE_ID / "release.yaml").read_bytes(), release_before
        )
        self.assertFalse((self.fixture.registry_root / ".staging").exists())

    def test_failed_finalisation_does_not_create_a_candidate(self) -> None:
        result = _run_cli(self.fixture, fail_after=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())
        self.assertFalse((self.fixture.registry_root / ".staging").exists())

    # -- accounting ------------------------------------------------------------

    def _run_emit_only(self) -> subprocess.CompletedProcess:
        return _run_cli(self.fixture, "--stage", "emit")

    def test_missing_record_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        before = _candidate_tables_bytes(self.fixture.registry_root)
        records = self.fixture.work_root / STORE_ID / "resolver/records"
        (records / "GCST90000002.json").unlink()
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stderr)
        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)
        self.assertFalse((self.fixture.registry_root / ".staging").exists())

    def test_extra_record_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        records = self.fixture.work_root / STORE_ID / "resolver/records"
        (records / "GCST99999999.json").write_text("{}", encoding="utf-8")
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra resolver record", result.stderr)

    def test_record_schema_version_mismatch_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        records = self.fixture.work_root / STORE_ID / "resolver/records"
        record_path = records / "GCST90000001.json"
        record = json.loads(record_path.read_text())
        record["record_schema_version"] = 2
        record_path.write_text(json.dumps(record), encoding="utf-8")
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("record_schema_version", result.stderr)

    def test_index_schema_version_mismatch_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        records = self.fixture.work_root / STORE_ID / "resolver/records"
        index_path = records / "index.json"
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
        self.assertFalse((self.fixture.registry_root / ".staging").exists())

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
        records = self.fixture.work_root / STORE_ID / "resolver/records"
        for analysis_id in receipt["analysis_ids"]:
            record = json.loads((records / f"{analysis_id}.json").read_text())
            self.assertEqual(
                receipt["record_digests"][analysis_id],
                record["fingerprints"]["fingerprint_digest"],
            )
        self.assertIn("resolve-analyses", " ".join(receipt["resolver"]["argv"]))

    def test_tampered_fingerprint_with_valid_self_digest_is_rejected(self) -> None:
        # The exact hole review found: mutate a gate/evidence fingerprint input
        # and recompute a *valid* self-digest. The record is internally
        # consistent, so only the receipt can reject it.
        self._resolved_candidate()
        records = self.fixture.work_root / STORE_ID / "resolver/records"
        record_path = records / "GCST90000001.json"
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
                run_root / "records",
                config=config,
                manifest_path=run_root / "analyses.tsv",
                receipt_path=run_root / RECEIPT_FILENAME,
            )
        self.assertTrue(
            any("opengwasdb_resolver_sha256" in failure for failure in failures), failures
        )

    def test_stale_record_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        records = self.fixture.work_root / STORE_ID / "resolver/records"
        record_path = records / "GCST90000001.json"
        record = json.loads(record_path.read_text())
        record["fingerprints"]["source_recorded_sha256"] = "0" * 64
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
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

            # an extra record the manifest does not account for
            (records / "index.json").write_text(json.dumps(base_index))
            (records / "GCST99999999.json").write_text("{}")
            _, failures = account_records(rows, records)
            self.assertTrue(any("extra resolver record" in failure for failure in failures))

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
            path.write_text(json.dumps({"receipt_schema_version": 2}))
            with self.assertRaises(CandidateError):
                read_resolution_receipt(path)
            path.write_text(
                json.dumps({"receipt_schema_version": 1, "record_schema_version": 9})
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
            resume=True,
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
            self.fixture.work_root / STORE_ID / "resolver/records",
            config=config,
            manifest_path=self.fixture.work_root / STORE_ID / "resolver/analyses.tsv",
            receipt_path=self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME,
        )
        self.assertEqual(failures, [])
        index = json.loads(
            (self.fixture.work_root / STORE_ID / "resolver/records/index.json").read_text()
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

    def test_validate_candidate_analyses_rejects_blank_included_required_value(self) -> None:
        header = "analysis_id\tstored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\toriginal_effect_scale\toriginal_sd_method\tancestry_assignment_method\n"
        blank = "GCST1\tsd\ttotal\tanalysis_level\t\tsd\testimated_from_source_maf\taf_assigned\n"
        self.assertTrue(validate_candidate_analyses(header + blank))

    def test_staged_candidate_check_detects_invalid_bundle(self) -> None:
        staging_parent = self.fixture.root / "staging"
        store_dir = staging_parent / STORE_ID
        store_dir.mkdir(parents=True)
        (store_dir / "release.yaml").write_text("store_id: OGS-99001\n", encoding="utf-8")
        errors = check_staged_candidate(STORE_ID, staging_parent)
        self.assertTrue(errors)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
