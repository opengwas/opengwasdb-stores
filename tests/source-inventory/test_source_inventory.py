#!/usr/bin/env python3
"""Tests for the frozen Source Inventory seam (issues #151, #283, #287).

Covers the contracts Manifest Generation depends on:

1. the readiness vocabulary: every acquisition status is classified explicitly,
   `ok` and `already_present` are ready, nothing else is;
2. the freeze stage over a release config: the shared freeze writes the
   inventory for the config's Selection, a frozen inventory is never
   overwritten, one frozen for another Selection or by the pre-#283
   candidate-table freeze is refused, and a config with no connectors needs an
   inventory that already exists;
3. preflight passes on a healthy snapshot and reports the plan, and fails on a
   missing/changed source file, unreadable or mis-declared metadata, an edited
   inventory, an undeclared method tier, a missing required Reference Resource,
   a bad work root, an over-cap core count, a missing provenance sidecar, and a
   provenance that is not this release's; a ready row without a source URL is
   a warning, not a failure;
4. preflight never opens a GWAS-SSF association body (proven with a tripwire);
5. the pre-#287 config reader the concordance study keeps still reads its
   ordered acquisition passes, and the committed inventories and every
   declared reference resource in this repository stay consistent.

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
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

from manifest_generation.freeze import FreezeError  # noqa: E402
from manifest_generation.inventory import freeze_release  # noqa: E402
from manifest_generation.providers.gwas_catalog.write_acquisition_manifest import (  # noqa: E402
    ACQUISITION_MANIFEST_COLUMNS,
    AcquisitionPass,
    write_from_passes,
)
from manifest_generation.release_config import (  # noqa: E402
    RELEASE_CONFIG_VERSION,
    ReleaseConfigError,
    load_release_config,
)
from manifest_generation.source_inventory import (  # noqa: E402
    MAX_CORES,
    InventoryError,
    discover_reference_resources,
    load_release_configuration,
    preflight,
    read_inventory,
    render_preflight_summary,
)

#: The pre-#287 generator config the preregistered concordance study is bound to.
LEGACY_CONFIG = REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/config-full.yaml"
CANONICAL_TRAIT_MAPPING_TABLE = REPO_ROOT / "resources/reference-resources/canonical-trait-mapping-efo"
SELECTION = {"where": {"store_key": "hybrid__European"}}
SHIPPED_INVENTORY = REPO_ROOT / "resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-22.tsv"
SHIPPED_PROVENANCE = REPO_ROOT / "resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-22.meta.yaml"
#: The snapshot 2026-09-22 supersedes. It stays in git so the rescue delta
#: stays reviewable rather than being replaced in place (issue #151).
SUPERSEDED_INVENTORY = REPO_ROOT / "resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv"
#: The acquisition mirror the shipped inventory's path columns point into. It
#: exists only on the build host, never on a CI runner.
SOURCE_MIRROR = Path("/data/opengwasdb/raw/ebi-gwas-catalog")

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
    rescue_status: str | None = None
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
        base_status="data_failed",
        # The retry pass is authoritative for the rows it covers even when its
        # outcome is worse than the base pass's -- but only across non-ready
        # outcomes: a later pass may not regress an already-ready Analysis, which
        # the GWAS Catalog Acquisition Manifest writer refuses
        # (tests/freeze/test_gwas_catalog_acquisition_manifest.py).
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
        # A third pass covering a row the retry pass never re-checked: it exists so
        # the overlay tests can prove order, not to change the default freeze.
        rescue_status="data_failed",
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
    """A hermetic fixture workspace: mirror files, connectors, release config, work root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.mirror = root / "mirror"
        self.reference = root / "reference"
        self.work_root = root / "work"
        self.config_path = root / "config.yaml"
        self.legacy_config_path = root / "legacy-config.yaml"
        self.source_metadata_table_path = root / "source-metadata.tsv"
        self.acquisition_manifest_path = root / "acquisition-manifest.tsv"
        self.base_manifest_path = root / "base-manifest.tsv"
        self.retry_manifest_path = root / "retry-manifest.tsv"
        self.rescue_manifest_path = root / "rescue-manifest.tsv"
        self.inventory_path = root / "inventory" / "fixture-snapshot.tsv"
        self.provenance_path = root / "inventory" / "fixture-snapshot.meta.yaml"
        self._build_mirror()
        self._write_source_metadata_table()
        self._write_manifests()
        write_from_passes(self.configured_passes(), self.acquisition_manifest_path)
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

    def write_source_metadata_table(self, builds: Mapping[str, str] | None = None) -> None:
        self._write_source_metadata_table(builds)

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

    def _write_source_metadata_table(self, builds: Mapping[str, str] | None = None) -> None:
        """The fixture Source Metadata Table; ``builds`` overrides each Analysis's GRCh38."""
        builds = builds or {}
        columns = [
            "source_analysis_id", "source_trait_label", "source_ancestry_label",
            "source_superpopulation", "study_design", "sample_size", "source_genome_build",
            "publication_pmid", "store_key",
        ]
        lines = ["\t".join(columns)]
        for entry in FIXTURE_ANALYSES:
            lines.append(
                "\t".join(
                    [
                        entry.analysis_id,
                        f"trait {entry.analysis_id}",
                        "European",
                        "EUR",
                        entry.study_design,
                        entry.sample_size,
                        builds.get(entry.analysis_id, "GRCh38"),
                        f"1{entry.analysis_id[-1]}",
                        entry.store_key,
                    ]
                )
            )
        self.source_metadata_table_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
        rescue_rows = [
            self.manifest_row(entry, entry.rescue_status, seconds="12.5")
            for entry in FIXTURE_ANALYSES
            if entry.rescue_status
        ]
        self.base_manifest_path.write_text(_render_manifest(base_rows), encoding="utf-8")
        self.retry_manifest_path.write_text(_render_manifest(retry_rows), encoding="utf-8")
        self.rescue_manifest_path.write_text(_render_manifest(rescue_rows), encoding="utf-8")

    def configured_passes(self) -> list[AcquisitionPass]:
        """The GWAS Catalog download passes the fixture's Acquisition Manifest overlays."""
        return [
            AcquisitionPass(role="base", path=self.base_manifest_path),
            AcquisitionPass(role="retry_transient", path=self.retry_manifest_path),
        ]

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
        """A release config over this workspace's connector files."""
        return {
            "release_config_version": RELEASE_CONFIG_VERSION,
            "label": "fixture-release",
            "association_coverage": "gwas",
            "description": "A fixture release.",
            "source_collection": {
                "source_collection_id": "gwas-catalog-ssf",
                "connectors": {
                    "source_metadata_table": str(self.source_metadata_table_path),
                    "acquisition_manifest": str(self.acquisition_manifest_path),
                },
                "inventory": {
                    "snapshot_id": "fixture-snapshot",
                    "directory": str(self.inventory_path.parent),
                },
            },
            "selection": copy.deepcopy(SELECTION),
            # Ragged, so the workspace (passed as the repository root) needs no
            # Indexed Variant Subset resource; preflight reads no Build Recipe.
            "build": {
                "layout": "ragged",
                "completion_state": "observed_only",
                "command": "build-ragged-ssf",
                "options": {"source-reader-capability": "opengwasdb.gwas-ssf"},
                "post": {"top_hits": False, "overview": True},
            },
            "defaults": {
                "source_genome_build": "GRCh38",
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
            "release_policy": {"selection_superpopulation": "EUR"},
            "reference_resources": [
                {
                    "resource_id": "ukb-ancestry-mixture-hg38",
                    "kind": "ancestry_mixture",
                    "location": str(self.reference / "ref_freqs.hg38.tsv.gz"),
                    "fine_group_map": str(self.reference / "ancestry_groups.tsv"),
                    "location_kind": "external_file",
                },
                {
                    "resource_id": "canonical-trait-mapping-efo",
                    "kind": "trait_ontology_mapping",
                    "location": str(CANONICAL_TRAIT_MAPPING_TABLE),
                    "version": "3",
                },
            ],
            "ancestry_assignment": {"reference_resource_id": "ukb-ancestry-mixture-hg38"},
            "effect_scale_validation": {"reference_resources": []},
            "trait_ontology_mapping": {"reference_resource_id": "canonical-trait-mapping-efo"},
            # A host-independent floor: the fixture workspace is a temp directory.
            "runtime": {"cores": 8, "min_free_gb": 0.001},
            "output": {"work_root": str(self.work_root)},
        }

    def write_config(self, mutate=None) -> Path:
        document = self.config_document()
        if mutate is not None:
            mutate(document)
        self.config_path.write_text(
            yaml.safe_dump(document, sort_keys=False, default_flow_style=False), encoding="utf-8"
        )
        return self.config_path

    def config(self, mutate=None):
        return load_release_config(self.write_config(mutate), self.root)

    def legacy_config(self, mutate=None):
        """The pre-#287 config the concordance study reads, retargeted at this workspace."""
        document = copy.deepcopy(yaml.safe_load(LEGACY_CONFIG.read_text(encoding="utf-8")))
        if mutate is not None:
            mutate(document)
        self.legacy_config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return load_release_configuration(self.legacy_config_path, self.root)

    # -- commands under test ----------------------------------------------
    def freeze(self, *, frozen_at: str = FROZEN_AT, config=None):
        return freeze_release(config or self.config(), frozen_at=frozen_at)

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


class TestReadinessVocabulary(SourceInventoryTestCase):
    def test_only_ok_and_already_present_are_ready(self) -> None:
        self.workspace.freeze()
        rows = {row.analysis_id: row for row in read_inventory(self.workspace.inventory_path)}
        self.assertEqual(
            {analysis_id for analysis_id, row in rows.items() if row.ready},
            {"GCST90000001", "GCST90000003", "GCST90000004"},
        )
        self.assertEqual(rows["GCST90000004"].readiness_status, "already_present")
        self.assertEqual(rows["GCST90000008"].readiness_status, "dry_run")
        self.assertFalse(rows["GCST90000008"].ready)
        # Not selected: another store_key.
        self.assertNotIn("GCST90000010", rows)

    def test_a_verified_file_s_failure_is_known_and_never_ready(self) -> None:
        # ukb-b records identities instead of transfers, so a later verification
        # can fail in ways a download manifest never reports (#289). Each is a
        # known status, and none of them is ready: an explained absence.
        from manifest_generation.source_inventory import (
            KNOWN_READINESS_STATUSES,
            READY_STATUSES,
            SOURCE_VERIFICATION_STATUSES,
        )

        self.assertEqual(
            SOURCE_VERIFICATION_STATUSES,
            frozenset(
                {
                    "missing_file",
                    "missing_index",
                    "size_mismatch",
                    "checksum_mismatch",
                    "index_mismatch",
                    "bad_index",
                    "integrity_failed",
                }
            ),
        )
        self.assertLessEqual(SOURCE_VERIFICATION_STATUSES, KNOWN_READINESS_STATUSES)
        self.assertEqual(SOURCE_VERIFICATION_STATUSES & READY_STATUSES, frozenset())


class TestFreezeRelease(SourceInventoryTestCase):
    """The freeze stage over a release config (#283, #287)."""

    def test_freezes_the_configs_selection_with_its_connectors(self) -> None:
        outcome = self.workspace.freeze()
        self.assertTrue(outcome.frozen)
        provenance = yaml.safe_load(self.workspace.provenance_path.read_text(encoding="utf-8"))
        self.assertEqual(provenance["frozen_by"], "manifest_generation.freeze")
        self.assertEqual(provenance["selection"], SELECTION)
        self.assertEqual(provenance["source_collection_id"], "gwas-catalog-ssf")
        self.assertEqual(outcome.inventory_sha256, provenance["inventory_tsv_sha256"])
        self.assertEqual(provenance["rows"], 9)
        for suffix in (".source-metadata.tsv", ".acquisition-manifest.tsv"):
            self.assertTrue(self.workspace.inventory_path.with_name(f"fixture-snapshot{suffix}").is_file())

    def test_an_existing_inventory_for_this_release_is_never_rewritten(self) -> None:
        self.workspace.freeze()
        before = {
            path.name: path.read_bytes() for path in self.workspace.inventory_path.parent.iterdir()
        }
        again = self.workspace.freeze(frozen_at="2030-01-01T00:00:00Z")
        self.assertFalse(again.frozen)
        after = {
            path.name: path.read_bytes() for path in self.workspace.inventory_path.parent.iterdir()
        }
        self.assertEqual(before, after)

    def test_an_inventory_frozen_for_another_selection_is_refused(self) -> None:
        self.workspace.freeze()
        config = self.workspace.config(
            lambda document: document.update(selection={"analyses": ["GCST90000001"]})
        )
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze(config=config)
        self.assertIn("a changed Selection is a new freeze", str(caught.exception))

    def test_an_inventory_from_the_candidate_table_freeze_is_refused(self) -> None:
        self.workspace.freeze()
        provenance = yaml.safe_load(self.workspace.provenance_path.read_text(encoding="utf-8"))
        del provenance["frozen_by"]
        self.workspace.provenance_path.write_text(yaml.safe_dump(provenance), encoding="utf-8")
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze()
        self.assertIn("not written by the shared freeze", str(caught.exception))

    def test_a_partial_freeze_is_never_overwritten(self) -> None:
        self.workspace.inventory_path.parent.mkdir(parents=True)
        self.workspace.inventory_path.write_text("leftover\n", encoding="utf-8")
        with self.assertRaises(FreezeError) as caught:
            self.workspace.freeze()
        self.assertIn("never overwritten", str(caught.exception))

    def test_a_config_without_connectors_needs_an_existing_inventory(self) -> None:
        config = self.workspace.config(
            lambda document: document["source_collection"].pop("connectors")
        )
        with self.assertRaises(InventoryError) as caught:
            self.workspace.freeze(config=config)
        self.assertIn("names no source_collection.connectors", str(caught.exception))
        # Once frozen (by a config that has them), the config without them reads it.
        self.workspace.freeze()
        self.assertFalse(self.workspace.freeze(config=config).frozen)


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
        self.assertEqual(
            report["required_reference_resources"],
            ["ukb-ancestry-mixture-hg38", "canonical-trait-mapping-efo"],
        )
        self.assertEqual(
            report["selection"],
            {
                "source_collection_id": "gwas-catalog-ssf",
                "selection": SELECTION,
                "expect_rows": None,
                "config_path": str(self.workspace.config_path),
            },
        )
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
        summary = render_preflight_summary(report)
        self.assertIn("OK: preflight passed", summary)
        self.assertIn("stale snapshot       1 non-ready row(s) have files on disk", summary)
        self.assertIn("WARNINGS:", summary)
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
                lambda document: document["source_collection"]["inventory"].update(
                    {"snapshot_id": "other-snapshot"}
                )
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

        with self.assertRaises(ReleaseConfigError) as caught:
            self.workspace.config(undeclare)
        self.assertIn("not a declared Reference Resource", str(caught.exception))

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
            ["ukb-ancestry-mixture-hg38", "fixture-eur-af", "canonical-trait-mapping-efo"],
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

    def test_preflight_fails_when_ready_row_missing_required_fields(self) -> None:
        path = self.workspace.inventory_path
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        fields = lines[1].split("\t")
        self.assertEqual(fields[0], "GCST90000001")
        fields[10] = ""
        lines[1] = "\t".join(fields)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        self.assertIn("missing required field(s)", " ".join(result.failures))
        self.assertIn("GCST90000001", " ".join(result.failures))

    def test_preflight_fails_when_sidecar_accounting_mismatches(self) -> None:
        provenance = yaml.safe_load(self.workspace.provenance_path.read_text(encoding="utf-8"))
        provenance["rows"] = 999
        self.workspace.provenance_path.write_text(
            yaml.safe_dump(provenance, sort_keys=False), encoding="utf-8"
        )
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        self.assertIn("records 999", " ".join(result.failures))

    def test_preflight_warns_when_required_reference_resource_lacks_version(self) -> None:
        result = self.workspace.preflight()
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(
            result.warnings,
            ("required Reference Resource 'ukb-ancestry-mixture-hg38' (ancestry_mixture) has no declared version",),
        )
        self.assertEqual(
            result.report["warnings"],
            ["required Reference Resource 'ukb-ancestry-mixture-hg38' (ancestry_mixture) has no declared version"],
        )
        summary = render_preflight_summary(result.report)
        self.assertIn("WARNINGS:", summary)
        self.assertIn("has no declared version", summary)


class TestPreflightReconciliation(SourceInventoryTestCase):
    """What the Acquisition Manifest leaves optional, preflight does not require (#287)."""

    def rewrite_manifest(self, edit) -> None:
        text = self.workspace.acquisition_manifest_path.read_text(encoding="utf-8")
        lines = [line.split("\t") for line in text.splitlines()]
        header = lines[0]
        rows = [dict(zip(header, line)) for line in lines[1:]]
        for row in rows:
            edit(row)
        self.workspace.acquisition_manifest_path.write_text(
            "\n".join(["\t".join(header)] + ["\t".join(row[name] for name in header) for row in rows])
            + "\n",
            encoding="utf-8",
        )

    def test_a_ready_row_without_a_source_url_is_a_warning(self) -> None:
        def drop_url(row):
            if row["source_analysis_id"] == "GCST90000004":
                row["source_url"] = ""

        self.rewrite_manifest(drop_url)
        self.workspace.freeze()
        result = self.workspace.preflight()
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.report["source_files"]["no_source_url"], ["GCST90000004"])
        self.assertIn(
            "1 ready Analysis row(s) record no source URL", " ".join(result.warnings)
        )

    def test_a_ready_row_with_no_recorded_sidecar_is_counted_not_failed(self) -> None:
        def drop_sidecar(row):
            if row["source_analysis_id"] == "GCST90000004":
                row.update(sidecar_location="", sidecar_url="", sidecar_bytes="")

        self.rewrite_manifest(drop_sidecar)
        self.workspace.freeze()
        result = self.workspace.preflight()
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.report["source_files"]["no_metadata_sidecar"], ["GCST90000004"])

    def rewrite_sidecars(self, text: str) -> None:
        for analysis_id in ("GCST90000001", "GCST90000003", "GCST90000004"):
            self.workspace.yaml_path(self.workspace.analysis(analysis_id)).write_text(
                text.format(analysis_id=analysis_id), encoding="utf-8"
            )

    def test_a_declared_build_is_checked_against_the_releases_default(self) -> None:
        """The canonical build check: SMT ``source_genome_build`` vs the release default (#347)."""
        self.workspace.write_source_metadata_table(
            {"GCST90000001": "GRCh37", "GCST90000003": "GRCh37", "GCST90000004": "GRCh37"}
        )
        grch37 = self.workspace.config(
            lambda document: document["defaults"].update(source_genome_build="GRCh37")
        )
        self.workspace.freeze(config=grch37)
        result = self.workspace.preflight(config=grch37)
        self.assertTrue(result.ok, result.failures)
        # The same declarations against a GRCh38 release fail, naming the Analyses.
        result = self.workspace.preflight()
        self.assertFalse(result.ok)
        failures = " ".join(result.failures)
        self.assertIn("GCST90000001 (source_genome_build='GRCh37')", failures)
        self.assertIn("not the release's source_genome_build GRCh38", failures)
        self.assertEqual(len(result.report["source_files"]["metadata_gate_failed"]), 3)

    def test_preflight_does_not_read_the_sidecar_yaml(self) -> None:
        """A recorded sidecar need only exist: unparseable or contradictory YAML changes nothing (#347)."""
        self.workspace.freeze()
        self.rewrite_sidecars("genome_assembly: [GRCh37\n")  # unparseable
        result = self.workspace.preflight()
        self.assertTrue(result.ok, result.failures)
        self.rewrite_sidecars("genome_assembly: GRCh37\n")  # contradicts the SMT's GRCh38
        result = self.workspace.preflight()
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.report["source_files"]["metadata_gate_failed"], [])

    def test_an_analysis_declaring_no_build_is_counted_not_failed(self) -> None:
        self.workspace.write_source_metadata_table(
            {"GCST90000001": "", "GCST90000003": "", "GCST90000004": ""}
        )
        self.workspace.freeze()
        result = self.workspace.preflight()
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(
            result.report["source_files"]["metadata_build_undeclared"],
            ["GCST90000001", "GCST90000003", "GCST90000004"],
        )

    def test_a_selection_expect_rows_bound_is_checked_against_the_frozen_inventory(self) -> None:
        """A declared membership bound is a check on the Selection (#290)."""
        good = self.workspace.config(lambda document: document["selection"].update(expect_rows=9))
        self.workspace.freeze(config=good)
        result = self.workspace.preflight(config=good)
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.report["selection"]["expect_rows"], 9)

        drift = self.workspace.config(lambda document: document["selection"].update(expect_rows=10))
        result = self.workspace.preflight(config=drift)
        self.assertFalse(result.ok)
        self.assertIn("expects 10", " ".join(result.failures))

    def test_a_provenance_frozen_for_another_selection_fails(self) -> None:
        self.workspace.freeze()
        config = self.workspace.config(
            lambda document: document.update(selection={"analyses": ["GCST90000001"]})
        )
        result = self.workspace.preflight(config=config)
        self.assertFalse(result.ok)
        self.assertIn("a changed Selection is a new freeze", " ".join(result.failures))

    def test_a_provenance_for_another_source_collection_fails(self) -> None:
        self.workspace.freeze()
        config = self.workspace.config(
            lambda document: document["source_collection"].update(source_collection_id="other")
        )
        result = self.workspace.preflight(config=config)
        self.assertFalse(result.ok)
        self.assertIn("froze Source Collection 'gwas-catalog-ssf'", " ".join(result.failures))


class TestCommandLine(SourceInventoryTestCase):
    """The documented operator interface, exercised in process."""

    def setUp(self) -> None:
        super().setUp()
        from manifest_generation import inventory

        self.cli = inventory

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
        self.assertIn("Frozen: Source Inventory fixture-snapshot", output)
        self.assertIn("rows        9 (3 ready)", output)

        code, output = self.run_cli("freeze", "--config", str(self.workspace.config_path))
        self.assertEqual(code, 0, output)
        self.assertIn("Already frozen for this release", output)

    def test_freeze_command_reports_a_refusal_without_a_traceback(self) -> None:
        self.workspace.write_config(lambda document: document["source_collection"].pop("connectors"))
        code, output = self.run_cli("freeze", "--config", str(self.workspace.config_path))
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", output)
        self.assertIn("names no source_collection.connectors", output)

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
        self.assertIn("freeze it first with 'pixi run inventory-freeze --config", output)

    def test_cli_diagnoses_unusable_work_root_without_traceback(self) -> None:
        self.workspace.write_config()
        self.workspace.freeze()
        blocked = self.root / "blocked_work_root"
        blocked.write_text("not a directory", encoding="utf-8")
        code, output = self.run_cli(
            "preflight",
            "--config",
            str(self.workspace.config_path),
            "--work-root",
            str(blocked),
        )
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", output)
        self.assertIn("FAILED:", output)
        self.assertIn("work root", output)
        self.assertIn("is unusable", output)
        self.assertIn("ERROR: could not write report to", output)

    def test_cli_diagnoses_unusable_report_path_without_traceback(self) -> None:
        self.workspace.write_config()
        self.workspace.freeze()
        blocked = self.root / "blocked_file"
        blocked.write_text("not a directory", encoding="utf-8")
        unusable_report = blocked / "report.json"
        code, output = self.run_cli(
            "preflight",
            "--config",
            str(self.workspace.config_path),
            "--report",
            str(unusable_report),
        )
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", output)
        self.assertIn("ERROR: could not write report to", output)


class TestShippedReleaseArtifacts(unittest.TestCase):
    """The committed full-release config, inventory and sidecar must agree in CI."""

    def test_shipped_config_is_loadable_and_points_at_the_shipped_snapshot(self) -> None:
        config = load_release_configuration(LEGACY_CONFIG, REPO_ROOT)
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

    def test_the_superseded_snapshot_is_still_readable_and_records_the_rescue_delta(self) -> None:
        """The rescue added ready Analyses; it never removed one.

        Keeping both snapshots in git is what makes that checkable, so this
        asserts the delta's direction rather than only its size.
        """
        before = {row.analysis_id: row for row in read_inventory(SUPERSEDED_INVENTORY)}
        after = {row.analysis_id: row for row in read_inventory(SHIPPED_INVENTORY)}
        self.assertEqual(set(before), set(after))
        gained = [a for a in after if after[a].ready and not before[a].ready]
        lost = [a for a in after if before[a].ready and not after[a].ready]
        self.assertEqual(lost, [])
        self.assertEqual(len(gained), 213)

    @unittest.skipUnless(SOURCE_MIRROR.is_dir(), "Source mirror not present on this host")
    def test_a_recorded_local_path_always_names_a_file_that_exists(self) -> None:
        """Every path column is a mirror fact, not an intention.

        Acquisition resolves the remote names before it transfers anything, so
        a row that failed partway could otherwise keep a path to a file that was
        never written -- and preflight only opens the paths of ready rows, so
        nothing else would catch it.
        """
        claimed = [
            (row.analysis_id, column, value)
            for row in read_inventory(SHIPPED_INVENTORY)
            for column, value in (("data_file", row.data_file), ("yaml_file", row.yaml_file))
            if value and not Path(value).exists()
        ]
        self.assertEqual(claimed, [])

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
                "already_present": 213,
                "data_absent_upstream": 49,
                "header_rejected": 98,
                "metadata_rejected": 1,
                "missing_remote_harmonised_yaml": 1104,
                "ok": 4570,
            },
        )
        # The issued baseline plus the recorded rescue delta, restated here so a
        # changed snapshot is a deliberate edit rather than a silent replacement
        # (issue #151). The candidate pool itself never moves: 6,035 throughout.
        self.assertEqual(len(rows), 6035)
        self.assertEqual(len([row for row in rows if row.ready]), 4783)
        self.assertEqual(provenance["ready_bytes"], 1889305604793)
        self.assertEqual(provenance["ready_study_design_counts"], {"case-control": 1159, "quantitative": 3624})
        self.assertEqual(
            [group["analysis_ids"] for group in provenance["duplicate_content_groups"]],
            [["GCST90565871", "GCST90565872"], ["GCST90624704", "GCST90624705"]],
        )
        for row in rows:
            if row.readiness_status == "missing_remote_harmonised_yaml":
                # Nothing was written for these, so no local path is claimed.
                # The attempted names survive in the URL columns.
                self.assertEqual(row.data_file, "")
                self.assertEqual(row.yaml_file, "")
            if row.readiness_status == "data_absent_upstream":
                # The sidecar downloaded; the association file 404s. Only the
                # file that exists locally may claim a path.
                self.assertEqual(row.data_file, "")
                self.assertTrue(row.yaml_file)
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
