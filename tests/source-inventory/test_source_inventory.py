#!/usr/bin/env python3
"""Tests for the frozen Source Inventory seam (issue #151).

Covers the contracts Phase B depends on:

1. the retry-overlay precedence rule, and that the base/retry delta is recorded
   rather than silently replaced;
2. the readiness vocabulary: every acquisition status is classified explicitly,
   `ok` and `already_present` are ready, nothing else is;
3. accounting: duplicate ids, unknown statuses, candidates with no acquisition
   row, and acquisition rows outside the candidate pool all fail loudly;
4. determinism: two freezes of the same inputs produce identical bytes, and
   per-run download timing never reaches the frozen file;
5. duplicate-content groups are reported and preserved, never collapsed;
6. preflight passes on a healthy snapshot and reports the plan, and fails on a
   missing/changed source file, unreadable or mis-declared metadata, an edited
   inventory, an undeclared method tier, a missing required Reference Resource,
   a bad work root, an over-cap core count, and a missing provenance sidecar;
7. preflight never opens a GWAS-SSF association body (proven with a tripwire);
8. the shipped full-release config, inventory and provenance stay consistent,
   and every declared reference resource in this repository loads.

Run from the repository root:  python3 tests/source-inventory/test_source_inventory.py
"""

from __future__ import annotations

import builtins
import contextlib
import copy
import gzip
import hashlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from resources.generators.lib.source_inventory import (  # noqa: E402
    ACQUISITION_MANIFEST_COLUMNS,
    CANDIDATE_COLUMNS,
    INVENTORY_COLUMNS,
    MAX_CORES,
    InventoryError,
    PreflightConfigError,
    build_snapshot,
    discover_reference_resources,
    duplicate_content_groups,
    load_release_configuration,
    preflight,
    read_candidate_selection,
    read_inventory,
    render_preflight_summary,
    write_snapshot,
)

SHIPPED_CONFIG = REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/config-full.yaml"
SHIPPED_INVENTORY = REPO_ROOT / "resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv"
SHIPPED_PROVENANCE = REPO_ROOT / "resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.meta.yaml"

SOURCE_CONTENT = {
    "ALPHA": "chromosome\tbase_pair_location\teffect_allele\n1\t100\tA\n",
    "BETA": "chromosome\tbase_pair_location\teffect_allele\n1\t200\tC\n",
    "GAMMA": "chromosome\tbase_pair_location\teffect_allele\n2\t300\tG\n",
}

GOOD_METADATA = (
    "# Study meta-data\n"
    "gwas_id: {analysis_id}\n"
    "genome_assembly: GRCh38\n"
    "coordinate_system: 1-based\n"
    "is_harmonised: true\n"
)
BAD_METADATA = GOOD_METADATA.replace("GRCh38", "GRCh37")


@dataclass(frozen=True)
class FixtureAnalysis:
    """One fixture Analysis: candidate metadata, acquisition outcome, local files."""

    analysis_id: str
    study_design: str
    sample_size: str
    base_status: str
    retry_status: str | None = None
    content_key: str = ""
    file_name: str = ""
    write_data: bool = False
    write_metadata: bool = False
    metadata_bad: bool = False
    error: str = ""
    store_key: str = "hybrid__European"


FIXTURE_ANALYSES: tuple[FixtureAnalysis, ...] = (
    FixtureAnalysis(
        analysis_id="GCST90000001",
        study_design="quantitative",
        sample_size="1000",
        base_status="ok",
        content_key="ALPHA",
        # PMID/EFO-prefixed harmonised name, which acquisition resolves and the
        # inventory must therefore record rather than reconstruct.
        file_name="1-GCST90000001-EFO_0000001.h.tsv.gz",
        write_data=True,
        write_metadata=True,
    ),
    FixtureAnalysis(
        analysis_id="GCST90000002",
        study_design="case-control",
        sample_size="1000",
        base_status="ok",
        # The retry pass is authoritative even when it is worse: this file was
        # re-checked and its header still lacks the required columns.
        retry_status="header_rejected",
        content_key="BETA",
        file_name="GCST90000002.h.tsv.gz",
        write_data=True,
        write_metadata=True,
        error="missing required GWAS-SSF columns: beta",
    ),
    FixtureAnalysis(
        analysis_id="GCST90000003",
        study_design="quantitative",
        sample_size="2000",
        base_status="data_failed",
        # ...and the retry pass is what turns a failed transfer into a ready source.
        retry_status="ok",
        content_key="ALPHA",
        file_name="GCST90000003.h.tsv.gz",
        write_data=True,
        write_metadata=True,
    ),
    FixtureAnalysis(
        analysis_id="GCST90000004",
        study_design="case-control",
        sample_size="3000",
        base_status="already_present",
        content_key="GAMMA",
        file_name="GCST90000004.h.tsv.gz",
        write_data=True,
        write_metadata=True,
    ),
    FixtureAnalysis(
        analysis_id="GCST90000005",
        study_design="quantitative",
        sample_size="4000",
        base_status="missing_remote_harmonised_yaml",
    ),
    FixtureAnalysis(
        analysis_id="GCST90000006",
        study_design="quantitative",
        sample_size="5000",
        base_status="metadata_rejected",
        file_name="GCST90000006.h.tsv.gz",
        write_metadata=True,
        metadata_bad=True,
        error="metadata genome_assembly is not GRCh38 (GRCh37)",
    ),
    FixtureAnalysis(
        analysis_id="GCST90000007",
        study_design="case-control",
        sample_size="6000",
        base_status="yaml_failed",
        file_name="GCST90000007.h.tsv.gz",
        error="curl: (7) Failed to connect to ftp.ebi.ac.uk:443",
    ),
    FixtureAnalysis(
        analysis_id="GCST90000008",
        study_design="quantitative",
        sample_size="7000",
        base_status="dry_run",
        file_name="GCST90000008.h.tsv.gz",
    ),
    FixtureAnalysis(
        analysis_id="GCST90000009",
        study_design="case-control",
        sample_size="8000",
        base_status="error",
        error="RuntimeError('unexpected')",
    ),
    FixtureAnalysis(
        analysis_id="GCST90000010",
        study_design="quantitative",
        sample_size="9000",
        base_status="",
        store_key="other__Store",
    ),
)

FROZEN_AT = "2026-09-10T09:30:00Z"


class Workspace:
    """A hermetic fixture workspace: mirror files, config, inventory and work root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.mirror = root / "mirror"
        self.reference = root / "reference"
        self.work_root = root / "work"
        self.config_path = root / "config.yaml"
        self.candidates_path = root / "candidates.tsv"
        self.base_manifest_path = root / "base-manifest.tsv"
        self.retry_manifest_path = root / "retry-manifest.tsv"
        self.inventory_path = root / "inventory" / "fixture-snapshot.tsv"
        self.provenance_path = root / "inventory" / "fixture-snapshot.meta.yaml"
        self._build_mirror()
        self._write_candidates()
        self._write_manifests()
        self._write_reference()
        self.write_config()

    # -- fixtures ---------------------------------------------------------
    def analysis(self, analysis_id: str) -> FixtureAnalysis:
        for entry in FIXTURE_ANALYSES:
            if entry.analysis_id == analysis_id:
                return entry
        raise KeyError(analysis_id)

    def data_path(self, entry: FixtureAnalysis) -> Path:
        return self.mirror / entry.analysis_id / entry.file_name

    def yaml_path(self, entry: FixtureAnalysis) -> Path:
        return self.mirror / entry.analysis_id / f"{entry.file_name}-meta.yaml"

    def _build_mirror(self) -> None:
        for entry in FIXTURE_ANALYSES:
            if not entry.file_name:
                continue
            directory = self.mirror / entry.analysis_id
            directory.mkdir(parents=True, exist_ok=True)
            if entry.write_data:
                self.data_path(entry).write_text(SOURCE_CONTENT[entry.content_key], encoding="utf-8")
            if entry.write_metadata:
                template = BAD_METADATA if entry.metadata_bad else GOOD_METADATA
                self.yaml_path(entry).write_text(
                    template.format(analysis_id=entry.analysis_id), encoding="utf-8"
                )

    def _write_candidates(self) -> None:
        columns = list(CANDIDATE_COLUMNS) + ["PUBMED.ID", "DISEASE.TRAIT", "ancestry_group"]
        lines = ["\t".join(columns)]
        for entry in FIXTURE_ANALYSES:
            lines.append(
                "\t".join(
                    [
                        entry.analysis_id,
                        entry.store_key,
                        entry.study_design,
                        f"1{entry.analysis_id[-1]}",
                        f"trait {entry.analysis_id}",
                        "European",
                    ]
                )
            )
        self.candidates_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def manifest_row(
        self, entry: FixtureAnalysis, status: str, *, seconds: str = "1.0"
    ) -> dict[str, str]:
        ready = status in {"ok", "already_present"}
        data_path = self.data_path(entry) if entry.file_name else None
        yaml_path = self.yaml_path(entry) if entry.file_name else None
        row = {
            "analysis_id": entry.analysis_id,
            "publication_pmid": f"1{entry.analysis_id[-1]}",
            "trait": f"trait {entry.analysis_id}",
            "study_design": entry.study_design,
            "sample_size": entry.sample_size,
            "status": status,
            "data_url": f"https://example.invalid/{entry.analysis_id}",
            "yaml_url": f"https://example.invalid/{entry.analysis_id}-meta.yaml",
            "data_file": str(data_path) if data_path and entry.file_name and status != "missing_remote_harmonised_yaml" else "",
            "yaml_file": str(yaml_path) if yaml_path and entry.file_name and status != "missing_remote_harmonised_yaml" else "",
            "data_bytes": "",
            "yaml_bytes": "",
            "sha256": "",
            "error": entry.error,
            "seconds": seconds,
        }
        if status == "missing_remote_harmonised_yaml":
            row["data_file"] = ""
            row["yaml_file"] = ""
            row["error"] = ""
        if ready:
            row["data_bytes"] = str(self.data_path(entry).stat().st_size)
            row["yaml_bytes"] = str(self.yaml_path(entry).stat().st_size)
            row["sha256"] = hashlib.sha256(SOURCE_CONTENT[entry.content_key].encode()).hexdigest()
        return row

    def _write_manifests(self) -> None:
        base_rows = [
            self.manifest_row(entry, entry.base_status, seconds="1.1")
            for entry in FIXTURE_ANALYSES
            if entry.base_status
        ]
        retry_rows = [
            self.manifest_row(entry, entry.retry_status, seconds="9.9")
            for entry in FIXTURE_ANALYSES
            if entry.retry_status
        ]
        self.base_manifest_path.write_text(_render_manifest(base_rows), encoding="utf-8")
        self.retry_manifest_path.write_text(_render_manifest(retry_rows), encoding="utf-8")

    def _write_reference(self) -> None:
        self.reference.mkdir(parents=True, exist_ok=True)
        (self.reference / "ref_freqs.hg38.tsv.gz").write_bytes(
            gzip.compress(b"alid\tAFR\tEUR\n1:100:A:G\t0.1\t0.2\n")
        )
        (self.reference / "ancestry_groups.tsv").write_text(
            "fine_group\tsuper_population\nEUR\tEUR\n", encoding="utf-8"
        )

    # -- config -----------------------------------------------------------
    def config_document(self) -> dict:
        """The shipped full-release config, retargeted at this workspace."""
        document = copy.deepcopy(yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8")))
        document["source"]["inventory"].update(
            {
                "snapshot_id": "fixture-snapshot",
                "path": str(self.inventory_path),
                "provenance_path": str(self.provenance_path),
                "freeze_inputs": {
                    "base_manifest": str(self.base_manifest_path),
                    "retry_manifest": str(self.retry_manifest_path),
                },
            }
        )
        document["source"]["candidates"] = str(self.candidates_path)
        document["reference_resources"][0]["location"] = str(self.reference / "ref_freqs.hg38.tsv.gz")
        document["reference_resources"][0]["fine_group_map"] = str(self.reference / "ancestry_groups.tsv")
        document["runtime"]["cores"] = 8
        # A host-independent floor: the fixture workspace is a temp directory, so
        # the shipped default (50 GB) would vary with the machine it runs on.
        document["runtime"]["min_free_gb"] = 0.001
        document["output"]["work_root"] = str(self.work_root)
        return document

    def write_config(self, mutate=None) -> Path:
        document = self.config_document()
        if mutate is not None:
            mutate(document)
        self.config_path.write_text(
            yaml.safe_dump(document, sort_keys=False, default_flow_style=False), encoding="utf-8"
        )
        return self.config_path

    def config(self, mutate=None):
        return load_release_configuration(self.write_config(mutate), self.root)

    # -- commands under test ----------------------------------------------
    def freeze(self, *, frozen_at: str = FROZEN_AT):
        candidates = read_candidate_selection(self.candidates_path, "hybrid__European")
        snapshot = build_snapshot(
            snapshot_id="fixture-snapshot",
            source_collection_id="gwas-catalog-ssf",
            store_key="hybrid__European",
            ancestry_group="European",
            base_manifest=self.base_manifest_path,
            retry_manifest=self.retry_manifest_path,
            candidates=candidates,
            frozen_at=frozen_at,
        )
        write_snapshot(snapshot, self.inventory_path, self.provenance_path)
        return snapshot

    def preflight(self, config=None, **kwargs):
        config = config or self.config()
        rows = read_inventory(self.inventory_path)
        return preflight(
            inventory_path=self.inventory_path,
            provenance_path=self.provenance_path,
            rows=rows,
            config=config,
            repo_root=self.root,
            generated_at=FROZEN_AT,
            **kwargs,
        )


def _render_manifest(rows: list[dict[str, str]], extra_columns: tuple[str, ...] = ("seconds",)) -> str:
    """Render a manifest in the acquisition script's own column order.

    ``seconds`` is written here, as acquisition writes it, and is expected to be
    absent from the frozen inventory: download timing is not a release fact.
    """
    columns = list(ACQUISITION_MANIFEST_COLUMNS) + list(extra_columns)
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(row.get(name, "") for name in columns))
    return "\n".join(lines) + "\n"


def _write_raw_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    """Write a manifest whose rows may omit columns, to test the loud failures."""
    columns = list(rows[0])
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(str(row.get(name, "")) for name in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class SourceInventoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="og151-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = Workspace(self.root)


class TestFreeze(SourceInventoryTestCase):
    def test_retry_manifest_is_authoritative_for_the_rows_it_covers(self) -> None:
        snapshot = self.workspace.freeze()
        self.assertEqual(
            snapshot.readiness_counts,
            {
                "already_present": 1,
                "dry_run": 1,
                "error": 1,
                "header_rejected": 1,
                "metadata_rejected": 1,
                "missing_remote_harmonised_yaml": 1,
                "ok": 2,
                "yaml_failed": 1,
            },
        )
        statuses = {row.analysis_id: row.readiness_status for row in snapshot.rows}
        # Base said `ok`, retry said `header_rejected`: the retry wins.
        self.assertEqual(statuses["GCST90000002"], "header_rejected")
        # Base said `data_failed`, retry said `ok`: the retry wins.
        self.assertEqual(statuses["GCST90000003"], "ok")
        # A row the retry pass did not cover keeps the base outcome.
        self.assertEqual(statuses["GCST90000001"], "ok")
        self.assertEqual(statuses["GCST90000009"], "error")

        inputs = {entry.role: entry for entry in snapshot.inputs}
        self.assertEqual(inputs["base_manifest"].rows, 9)
        self.assertEqual(inputs["retry_manifest"].rows, 2)
        self.assertEqual(inputs["retry_manifest"].overrides, 2)
        # The delta between the two passes stays visible in the provenance.
        self.assertEqual(inputs["base_manifest"].readiness_counts["ok"], 2)
        self.assertEqual(inputs["base_manifest"].readiness_counts["data_failed"], 1)
        self.assertEqual(inputs["retry_manifest"].readiness_counts["header_rejected"], 1)

    def test_only_ok_and_already_present_are_ready(self) -> None:
        snapshot = self.workspace.freeze()
        ready = {row.analysis_id for row in snapshot.ready_rows}
        self.assertEqual(ready, {"GCST90000001", "GCST90000003", "GCST90000004"})
        self.assertEqual(snapshot.ready_study_design_counts, {"case-control": 1, "quantitative": 2})
        expected_bytes = 2 * len(SOURCE_CONTENT["ALPHA"]) + len(SOURCE_CONTENT["GAMMA"])
        self.assertEqual(snapshot.ready_bytes, expected_bytes)
        # Unavailable inputs stay inventory rows, not members.
        self.assertEqual(len(snapshot.rows), 9)
        self.assertEqual(
            {row.readiness_status for row in snapshot.rows},
            {
                "ok",
                "already_present",
                "header_rejected",
                "missing_remote_harmonised_yaml",
                "metadata_rejected",
                "yaml_failed",
                "dry_run",
                "error",
            },
        )

    def test_freeze_is_deterministic_and_drops_download_timing(self) -> None:
        first = self.workspace.freeze()
        text = self.workspace.inventory_path.read_text(encoding="utf-8")
        self.workspace.freeze()
        self.assertEqual(self.workspace.inventory_path.read_text(encoding="utf-8"), text)
        self.assertNotIn("seconds", first.rows[0].as_dict())
        self.assertIn("seconds", self.workspace.base_manifest_path.read_text(encoding="utf-8"))
        # Rows carry the columns downstream resolves from, in order.
        self.assertEqual(tuple(text.splitlines()[0].split("\t")), INVENTORY_COLUMNS)

    def test_records_exact_source_paths_for_both_filename_shapes(self) -> None:
        snapshot = self.workspace.freeze()
        rows = {row.analysis_id: row for row in snapshot.rows}
        prefixed = rows["GCST90000001"]
        canonical = rows["GCST90000004"]
        self.assertEqual(prefixed.data_file, str(self.workspace.data_path(self.workspace.analysis("GCST90000001"))))
        self.assertTrue(prefixed.data_file.endswith("1-GCST90000001-EFO_0000001.h.tsv.gz"))
        self.assertTrue(canonical.data_file.endswith("/GCST90000004.h.tsv.gz"))
        self.assertEqual(prefixed.data_url, "https://example.invalid/GCST90000001")
        self.assertEqual(
            prefixed.sha256, hashlib.sha256(SOURCE_CONTENT["ALPHA"].encode()).hexdigest()
        )

    def test_provenance_sidecar_records_identity_and_inputs(self) -> None:
        snapshot = self.workspace.freeze()
        provenance = yaml.safe_load(self.workspace.provenance_path.read_text(encoding="utf-8"))
        inventory_text = self.workspace.inventory_path.read_text(encoding="utf-8")
        self.assertEqual(provenance["snapshot_id"], "fixture-snapshot")
        self.assertEqual(provenance["rows"], 9)
        self.assertEqual(provenance["ready_rows"], 3)
        self.assertEqual(provenance["frozen_at"], FROZEN_AT)
        self.assertEqual(
            provenance["inventory_tsv_sha256"], hashlib.sha256(inventory_text.encode()).hexdigest()
        )
        self.assertEqual(
            {entry["role"] for entry in provenance["inputs"]}, {"base_manifest", "retry_manifest"}
        )
        self.assertEqual(provenance["candidates"]["selected_rows"], 9)
        self.assertEqual(provenance["duplicate_content_groups"], [group.as_dict() for group in snapshot.duplicates])

    def test_duplicate_content_is_reported_and_preserved(self) -> None:
        snapshot = self.workspace.freeze()
        groups = snapshot.duplicates
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].analysis_ids, ("GCST90000001", "GCST90000003"))
        self.assertEqual(groups[0].sha256, hashlib.sha256(SOURCE_CONTENT["ALPHA"].encode()).hexdigest())
        # Both accessions remain ready members: collapsing them is a human decision.
        self.assertIn("GCST90000001", {row.analysis_id for row in snapshot.ready_rows})
        self.assertIn("GCST90000003", {row.analysis_id for row in snapshot.ready_rows})
        self.assertEqual(len(duplicate_content_groups(snapshot.rows)), 1)

    def test_round_trip_through_the_written_tsv(self) -> None:
        snapshot = self.workspace.freeze()
        self.assertEqual(read_inventory(self.workspace.inventory_path), snapshot.rows)

    def test_freeze_fails_on_duplicate_analysis_id(self) -> None:
        rows = [self.workspace.manifest_row(self.workspace.analysis("GCST90000001"), "ok")] * 2
        _write_raw_manifest(self.workspace.retry_manifest_path, rows)
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze()
        self.assertIn("duplicate analysis_id", str(caught.exception))

    def test_freeze_fails_on_unknown_readiness_status(self) -> None:
        row = self.workspace.manifest_row(self.workspace.analysis("GCST90000001"), "ok")
        row["status"] = "probably_fine"
        _write_raw_manifest(self.workspace.retry_manifest_path, [row])
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze()
        self.assertIn("unknown readiness status", str(caught.exception))

    def test_freeze_fails_when_a_candidate_has_no_acquisition_row(self) -> None:
        rows = [
            self.workspace.manifest_row(self.workspace.analysis(entry.analysis_id), entry.base_status)
            for entry in FIXTURE_ANALYSES
            if entry.base_status and entry.analysis_id != "GCST90000009"
        ]
        _write_raw_manifest(self.workspace.base_manifest_path, rows)
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze()
        self.assertIn("have no acquisition row", str(caught.exception))
        self.assertIn("GCST90000009", str(caught.exception))

    def test_freeze_fails_on_a_manifest_row_outside_the_candidate_pool(self) -> None:
        extra = self.workspace.manifest_row(self.workspace.analysis("GCST90000001"), "ok")
        extra["analysis_id"] = "GCST90009999"
        rows = [
            self.workspace.manifest_row(self.workspace.analysis(entry.analysis_id), entry.base_status)
            for entry in FIXTURE_ANALYSES
            if entry.base_status
        ] + [extra]
        _write_raw_manifest(self.workspace.base_manifest_path, rows)
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze()
        self.assertIn("outside the", str(caught.exception))
        self.assertIn("GCST90009999", str(caught.exception))

    def test_freeze_fails_on_duplicate_candidate_accession(self) -> None:
        text = self.workspace.candidates_path.read_text(encoding="utf-8")
        body = text.splitlines()[1]
        self.workspace.candidates_path.write_text(text + body + "\n", encoding="utf-8")
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze()
        self.assertIn("duplicate STUDY.ACCESSION", str(caught.exception))

    def test_freeze_fails_on_missing_required_manifest_column(self) -> None:
        rows = [
            self.workspace.manifest_row(self.workspace.analysis(entry.analysis_id), entry.base_status)
            for entry in FIXTURE_ANALYSES
            if entry.base_status
        ]
        for row in rows:
            row.pop("sha256")
        _write_raw_manifest(self.workspace.base_manifest_path, rows)
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze()
        self.assertIn("missing required column", str(caught.exception))

    def test_candidate_table_is_required_and_the_error_says_how_to_make_it(self) -> None:
        missing = self.root / "no-such-candidates.tsv"
        with self.assertRaises(InventoryError) as caught:
            read_candidate_selection(missing, "hybrid__European")
        self.assertIn("ebi-studies.r", str(caught.exception))


class TestPreflight(SourceInventoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace.freeze()

    def test_passes_and_reports_the_plan(self) -> None:
        result = self.workspace.preflight()
        self.assertTrue(result.ok, result.failures)
        report = result.report
        self.assertEqual(report["inventory"]["rows"], 9)
        self.assertEqual(report["inventory"]["ready_rows"], 3)
        self.assertEqual(report["inventory"]["snapshot_id"], "fixture-snapshot")
        self.assertEqual(
            report["inventory"]["readiness_counts"],
            {
                "already_present": 1,
                "dry_run": 1,
                "error": 1,
                "header_rejected": 1,
                "metadata_rejected": 1,
                "missing_remote_harmonised_yaml": 1,
                "ok": 2,
                "yaml_failed": 1,
            },
        )
        self.assertEqual(report["plan"]["planned_tier_counts"], {"case-control": 1, "quantitative": 2})
        self.assertEqual(
            report["plan"]["method_tiers"]["quantitative"]["original_sd_method"],
            "estimated_from_source_maf",
        )
        self.assertEqual(
            report["plan"]["method_tiers"]["case-control"]["stored_effect_scale"], "log_or"
        )
        self.assertEqual(
            report["plan"]["expected_exclusions"],
            {
                "readiness:dry_run": 1,
                "readiness:error": 1,
                "readiness:header_rejected": 1,
                "readiness:metadata_rejected": 1,
                "readiness:missing_remote_harmonised_yaml": 1,
                "readiness:yaml_failed": 1,
            },
        )
        self.assertEqual(report["plan"]["cores"], 8)
        self.assertEqual(report["plan"]["cores_source"], "config")
        self.assertEqual(report["plan"]["reference_af_fallback_policy"], "none_declared")
        self.assertTrue(report["work_root"]["writable"])
        self.assertGreater(report["work_root"]["free_gb"], 0)
        self.assertEqual(report["required_reference_resources"], ["ukb-ancestry-mixture-hg38"])
        required = [
            resource
            for resource in report["reference_resources"]
            if resource["resource_id"] == "ukb-ancestry-mixture-hg38"
        ]
        self.assertEqual(len(required), 1)
        self.assertTrue(required[0]["required"])
        self.assertTrue(required[0]["present"])
        self.assertTrue(required[0]["fine_group_map_present"])
        self.assertGreater(required[0]["bytes"], 0)
        self.assertEqual(report["source_files"]["checked"], 3)
        # The header-rejected file stays on disk, which is expected and is
        # reported so a re-frozen snapshot is a deliberate act.
        self.assertEqual(report["source_files"]["not_ready_with_present_file"], ["GCST90000002"])
        self.assertIn("OK: preflight passed", render_preflight_summary(report))
        # The report is JSON-serialisable evidence.
        json.dumps(report)

    def test_never_opens_an_association_body(self) -> None:
        real_open, real_io_open, real_gzip_open = builtins.open, io.open, gzip.open

        def guarded(original, *args, **kwargs):
            def wrapper(file, *rest, **rest_kwargs):
                name = str(file)
                if name.endswith(".h.tsv.gz") or name.endswith(".bgz"):
                    raise AssertionError(f"preflight opened an association body: {name}")
                return original(file, *rest, **rest_kwargs)

            return wrapper

        def guarded_gzip(file, *args, **kwargs):
            if str(file).endswith((".h.tsv.gz", ".bgz")):
                raise AssertionError(f"preflight gzip-opened an association body: {file}")
            return real_gzip_open(file, *args, **kwargs)

        builtins.open = guarded(real_open)
        io.open = guarded(real_io_open)
        gzip.open = guarded_gzip
        try:
            result = self.workspace.preflight()
        finally:
            builtins.open, io.open, gzip.open = real_open, real_io_open, real_gzip_open
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.report["source_files"]["checked"], 3)

    def test_fails_when_a_ready_source_file_is_missing(self) -> None:
        self.workspace.data_path(self.workspace.analysis("GCST90000001")).unlink()
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        self.assertIn("source file(s) are missing", " ".join(result.failures))
        self.assertEqual(result.report["source_files"]["missing"], ["GCST90000001"])

    def test_fails_when_a_ready_source_file_changed_size(self) -> None:
        path = self.workspace.data_path(self.workspace.analysis("GCST90000004"))
        path.write_text(SOURCE_CONTENT["GAMMA"] + "1\t400\tT\n", encoding="utf-8")
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        self.assertIn("changed size since the freeze", " ".join(result.failures))
        self.assertEqual(len(result.report["source_files"]["size_mismatch"]), 1)

    def test_fails_when_metadata_is_missing_unreadable_or_misdeclared(self) -> None:
        unreadable = self.workspace.yaml_path(self.workspace.analysis("GCST90000001"))
        unreadable.write_text("genome_assembly: [GRCh38\n", encoding="utf-8")
        (self.workspace.yaml_path(self.workspace.analysis("GCST90000004"))).write_text(
            BAD_METADATA.format(analysis_id="GCST90000004"), encoding="utf-8"
        )
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        failures = " ".join(result.failures)
        self.assertIn("metadata file(s) are unreadable", failures)
        self.assertIn("no longer declare GRCh38", failures)
        self.assertEqual(result.report["source_files"]["metadata_unreadable"], ["GCST90000001 (ParserError)"])
        self.assertEqual(len(result.report["source_files"]["metadata_gate_failed"]), 1)

    def test_fails_when_metadata_is_absent(self) -> None:
        self.workspace.yaml_path(self.workspace.analysis("GCST90000003")).unlink()
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        self.assertIn("metadata file(s) are missing", " ".join(result.failures))

    def test_fails_when_the_frozen_inventory_was_edited(self) -> None:
        path = self.workspace.inventory_path
        text = path.read_text(encoding="utf-8")
        # Flip a not-ready row to `ok`: a plausible-looking edit that would
        # promote a header-rejected source into membership.
        self.assertIn("\theader_rejected\t", text)
        path.write_text(text.replace("\theader_rejected\t", "\tok\t"), encoding="utf-8")
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        self.assertIn("edited after freezing", " ".join(result.failures))
        self.assertNotEqual(
            result.report["inventory"]["sha256"], result.report["inventory"]["provenance_sha256"]
        )

    def test_fails_when_the_provenance_sidecar_is_missing(self) -> None:
        self.workspace.provenance_path.unlink()
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        self.assertIn("provenance sidecar is missing", " ".join(result.failures))

    def test_fails_when_the_snapshot_is_not_the_one_the_release_declares(self) -> None:
        result = self.workspace.preflight(
            config=self.workspace.config(
                lambda document: document["source"]["inventory"].update({"snapshot_id": "other-snapshot"})
            )
        )
        self.assertFalse(result.ok)
        self.assertIn("does not match the snapshot this release declares", " ".join(result.failures))

    def test_fails_when_a_study_design_has_no_declared_method_tier(self) -> None:
        def drop_quantitative(document):
            del document["defaults"]["by_study_design"]["quantitative"]

        result = self.workspace.preflight(config=self.workspace.config(drop_quantitative))
        self.assertFalse(result.ok)
        self.assertIn("no declared method tier for study_design", " ".join(result.failures))
        self.assertIn("'quantitative' (2 ready Analyses)", " ".join(result.failures))

    def test_fails_when_a_required_reference_resource_is_absent(self) -> None:
        result = self.workspace.preflight(
            config=self.workspace.config(
                lambda document: document["reference_resources"][0].update(
                    {"location": str(self.root / "no-such-panel.tsv.gz")}
                )
            )
        )
        self.assertFalse(result.ok)
        self.assertIn("is not present at", " ".join(result.failures))

    def test_fails_when_the_ancestry_fine_group_map_is_absent(self) -> None:
        result = self.workspace.preflight(
            config=self.workspace.config(
                lambda document: document["reference_resources"][0].update(
                    {"fine_group_map": str(self.root / "no-such-map.tsv")}
                )
            )
        )
        self.assertFalse(result.ok)
        self.assertIn("missing its fine_group_map", " ".join(result.failures))

    def test_fails_when_a_referenced_resource_is_not_declared(self) -> None:
        def undeclare(document):
            document["ancestry_assignment"]["reference_resource_id"] = "not-declared"

        with self.assertRaises(PreflightConfigError) as caught:
            self.workspace.config(undeclare)
        self.assertIn("does not declare", str(caught.exception))

    def test_reports_a_declared_fallback_when_one_is_configured(self) -> None:
        def add_fallback(document):
            document["reference_resources"].append(
                {
                    "resource_id": "fixture-eur-af",
                    "kind": "reference_af",
                    "ancestry": "EUR",
                    "location": str(self.workspace.reference / "ref_freqs.hg38.tsv.gz"),
                    "location_kind": "external_file",
                }
            )
            document["effect_scale_validation"]["reference_resources"] = [
                {"ancestry": "EUR", "resource_id": "fixture-eur-af"}
            ]

        result = self.workspace.preflight(config=self.workspace.config(add_fallback))
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.report["plan"]["reference_af_fallback_policy"], "declared")
        self.assertEqual(
            result.report["required_reference_resources"],
            ["ukb-ancestry-mixture-hg38", "fixture-eur-af"],
        )

    def test_rejects_a_core_count_above_the_release_cap(self) -> None:
        result = self.workspace.preflight(cores=MAX_CORES + 1)
        self.assertFalse(result.ok)
        self.assertIn("exceeds the", " ".join(result.failures))
        lower = self.workspace.preflight(cores=4)
        self.assertTrue(lower.ok, lower.failures)
        self.assertEqual(lower.report["plan"]["cores"], 4)
        self.assertEqual(lower.report["plan"]["cores_source"], "cli")

    def test_rejects_a_non_positive_core_count(self) -> None:
        result = self.workspace.preflight(cores=0)
        self.assertFalse(result.ok)
        self.assertIn("must be at least 1", " ".join(result.failures))

    def test_fails_when_the_work_root_is_unusable(self) -> None:
        blocked = self.root / "not-a-directory"
        blocked.write_text("in the way", encoding="utf-8")
        result = self.workspace.preflight(
            config=self.workspace.config(
                lambda document: document["output"].update({"work_root": str(blocked)})
            )
        )
        self.assertFalse(result.ok)
        self.assertIn("is unusable", " ".join(result.failures))

    def test_fails_when_free_space_is_below_the_declared_minimum(self) -> None:
        result = self.workspace.preflight(
            config=self.workspace.config(
                lambda document: document["runtime"].update({"min_free_gb": 1e9})
            )
        )
        self.assertFalse(result.ok)
        self.assertIn("below the declared minimum", " ".join(result.failures))

    def test_creates_a_missing_work_root_and_says_so(self) -> None:
        result = self.workspace.preflight()
        self.assertTrue(result.ok, result.failures)
        self.assertTrue(result.report["work_root"]["created"])
        self.assertTrue(self.workspace.work_root.is_dir())

    def test_fails_when_the_inventory_itself_has_an_unknown_status(self) -> None:
        path = self.workspace.inventory_path
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("\theader_rejected\t", "\tprobably_fine\t"), encoding="utf-8")
        with self.assertRaises(InventoryError) as caught:
            self.workspace.preflight()
        self.assertIn("unknown readiness_status", str(caught.exception))


class TestCommandLine(SourceInventoryTestCase):
    """The documented operator interface, exercised in process."""

    def setUp(self) -> None:
        super().setUp()
        import importlib.util

        path = REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/inventory.py"
        spec = importlib.util.spec_from_file_location("gwas_catalog_eur_hybrid_inventory", path)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = self.cli.main(["--repo-root", str(self.root), *argv])
        return code, buffer.getvalue()

    def test_freeze_command_writes_the_configured_snapshot(self) -> None:
        self.workspace.write_config()
        code, output = self.run_cli(
            "freeze", "--config", str(self.workspace.config_path), "--frozen-at", FROZEN_AT
        )
        self.assertEqual(code, 0, output)
        self.assertTrue(self.workspace.inventory_path.is_file())
        self.assertTrue(self.workspace.provenance_path.is_file())
        self.assertIn("ready       3 Analyses", output)
        self.assertIn("GCST90000001 = GCST90000003", output)

    def test_freeze_command_names_a_new_snapshot_beside_the_configured_one(self) -> None:
        self.workspace.write_config()
        code, output = self.run_cli(
            "freeze",
            "--config",
            str(self.workspace.config_path),
            "--snapshot-id",
            "fixture-snapshot-next",
            "--frozen-at",
            FROZEN_AT,
        )
        self.assertEqual(code, 0, output)
        self.assertTrue(self.workspace.inventory_path.with_name("fixture-snapshot-next.tsv").is_file())
        self.assertTrue(
            self.workspace.inventory_path.with_name("fixture-snapshot-next.meta.yaml").is_file()
        )
        # The configured snapshot is untouched: re-freezing is explicit.
        self.assertFalse(self.workspace.inventory_path.exists())

    def test_preflight_command_writes_a_report_and_exits_nonzero_on_failure(self) -> None:
        self.workspace.write_config()
        self.workspace.freeze()
        code, output = self.run_cli("preflight", "--config", str(self.workspace.config_path))
        self.assertEqual(code, 0, output)
        report_path = self.workspace.work_root / "preflight" / "fixture-snapshot.json"
        self.assertTrue(report_path.is_file())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["plan"]["cores"], 8)

        self.workspace.data_path(self.workspace.analysis("GCST90000001")).unlink()
        code, output = self.run_cli("preflight", "--config", str(self.workspace.config_path))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", output)
        self.assertIn("source file(s) are missing", output)

    def test_cli_reports_a_missing_config_without_a_traceback(self) -> None:
        code, output = self.run_cli("preflight", "--config", str(self.root / "no-such-config.yaml"))
        self.assertEqual(code, 1)
        self.assertIn("ERROR: release config not found", output)

    def test_cli_reports_a_missing_inventory_and_how_to_make_it(self) -> None:
        self.workspace.write_config()
        code, output = self.run_cli("preflight", "--config", str(self.workspace.config_path))
        self.assertEqual(code, 1)
        self.assertIn("freeze it first with 'pixi run inventory-freeze'", output)


class TestShippedReleaseArtifacts(unittest.TestCase):
    """The committed full-release config, inventory and sidecar must agree in CI."""

    def test_shipped_config_is_loadable_and_points_at_the_shipped_snapshot(self) -> None:
        config = load_release_configuration(SHIPPED_CONFIG, REPO_ROOT)
        self.assertEqual(config.store_key, "hybrid__European")
        self.assertEqual(config.source_collection_id, "gwas-catalog-ssf")
        self.assertEqual(config.ancestry_group, "European")
        self.assertEqual(config.inventory_path, SHIPPED_INVENTORY)
        self.assertEqual(config.inventory_provenance_path, SHIPPED_PROVENANCE)
        self.assertEqual(config.inventory_path.stem, config.inventory_snapshot_id)
        self.assertEqual(config.cores, 64)
        self.assertLessEqual(config.cores, MAX_CORES)
        self.assertEqual(config.required_resource_ids, ("ukb-ancestry-mixture-hg38",))
        # Source-AF only until issue #152 decides otherwise: no reference-AF
        # fallback is declared, so an unusable source AF is an explicit skip.
        self.assertEqual(config.effect_scale_reference_resources, ())
        self.assertEqual(
            set(config.method_tiers), {"quantitative", "case-control"}
        )
        self.assertEqual(config.method_tiers["quantitative"].stored_effect_scale, "sd")
        self.assertEqual(config.method_tiers["case-control"].stored_effect_scale, "log_or")
        self.assertEqual(
            set(config.freeze_inputs), {"base_manifest", "retry_manifest"}
        )

    def test_shipped_provenance_matches_the_shipped_inventory(self) -> None:
        provenance = yaml.safe_load(SHIPPED_PROVENANCE.read_text(encoding="utf-8"))
        text = SHIPPED_INVENTORY.read_text(encoding="utf-8")
        self.assertEqual(
            provenance["inventory_tsv_sha256"], hashlib.sha256(text.encode()).hexdigest()
        )
        self.assertEqual(provenance["snapshot_id"], SHIPPED_INVENTORY.stem)
        rows = read_inventory(SHIPPED_INVENTORY)
        self.assertEqual(provenance["rows"], len(rows))
        self.assertEqual(provenance["ready_rows"], len([row for row in rows if row.ready]))
        self.assertEqual(
            provenance["readiness_counts"],
            {
                "data_failed": 52,
                "header_rejected": 278,
                "metadata_rejected": 1,
                "missing_remote_harmonised_yaml": 1134,
                "ok": 4570,
            },
        )
        # The issued baseline, restated here so a changed snapshot is a deliberate
        # edit rather than a silent replacement (issue #151).
        self.assertEqual(len(rows), 6035)
        self.assertEqual(len([row for row in rows if row.ready]), 4570)
        self.assertEqual(provenance["ready_bytes"], 1766662881302)
        self.assertEqual(provenance["ready_study_design_counts"], {"case-control": 1057, "quantitative": 3513})
        self.assertEqual(
            [group["analysis_ids"] for group in provenance["duplicate_content_groups"]],
            [["GCST90565871", "GCST90565872"], ["GCST90624704", "GCST90624705"]],
        )
        for row in rows:
            if row.readiness_status == "missing_remote_harmonised_yaml":
                # Acquisition never resolved a path, so both are blank rather
                # than reconstructed from the accession.
                self.assertEqual(row.data_file, "")
                self.assertEqual(row.yaml_file, "")
            if row.ready:
                self.assertTrue(row.data_file and row.data_url and row.sha256)
                self.assertIsNotNone(row.recorded_bytes)
                self.assertGreater(row.recorded_bytes, 0)

    def test_every_declared_reference_resource_loads(self) -> None:
        resources = discover_reference_resources(REPO_ROOT)
        for resource_id in (
            "ukb-ancestry-mixture-hg38",
            "ukb-hg38-eur-af",
            "qc-panel-hg38",
            "canonical-trait-mapping-efo",
        ):
            self.assertIn(resource_id, resources)
        self.assertTrue(resources["ukb-ancestry-mixture-hg38"].auxiliary_paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
