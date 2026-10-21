#!/usr/bin/env python3
"""The Analysis Catalog generation entry point (#323, ADR 0039).

Fixture Release Bundles go into a temporary registry; the catalog is generated
into a temporary Artifact Root; the assertions are SQL on the produced database
and checks of the files under `catalog/`.

Run from the repository root:
    python3 tests/catalog/test_catalog.py
"""

from __future__ import annotations

import csv
import gzip
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

import yaml

from analysis_catalog import build as catalog_build
from analysis_catalog import publish as catalog_publish

REQUIRED_COLUMNS = (
    "analysis_id", "stored_effect_scale", "sample_size_kind", "sample_size_scope",
    "sample_size", "original_effect_scale", "original_sd_method",
    "assigned_ancestry", "ancestry_assignment_method", "checksum",
    "checksum_algorithm", "source_file",
)


def skip_summary(version_dir: Path) -> None:
    """The renderer of every test but the smoke render: the summary needs Quarto."""


def build_catalog(registry: Path, root: Path, **kwargs: Any) -> catalog_build.BuildResult:
    kwargs.setdefault("renderer", skip_summary)
    return catalog_build.build_catalog(registry, root, **kwargs)


def analysis_row(analysis_id: str, **extra: str) -> dict[str, str]:
    row = {
        "analysis_id": analysis_id,
        "stored_effect_scale": "sd",
        "sample_size_kind": "total",
        "sample_size_scope": "analysis_level",
        "sample_size": "1000",
        "original_effect_scale": "sd",
        "original_sd_method": "declared_standardised",
        "assigned_ancestry": "EUR",
        "ancestry_assignment_method": "af_assigned",
        "checksum": "a" * 64,
        "checksum_algorithm": "sha256",
        "source_file": "/data/source/fixture.tsv",
    }
    row.update(extra)
    return row


def write_bundle(
    registry: Path,
    store_id: str,
    *,
    status: str = "validated",
    release: dict[str, Any] | None = None,
    n_analyses: Any = "match",
    rows: list[dict[str, str]] | None = None,
    validation: bool = True,
    observed_extra: dict[str, Any] | None = None,
) -> None:
    root = registry / store_id
    root.mkdir(parents=True, exist_ok=True)
    rows = rows if rows is not None else [analysis_row("A1"), analysis_row("A2")]
    release_data: dict[str, Any] = {
        "store_id": store_id,
        "label": f"label-{store_id}",
        "status": status,
        "access_posture": "public",
        "source_snapshot_id": "fixture-snapshot-v1",
        "derived_from": None,
        "created_at": "2026-09-17T00:00:00Z",
        "description": "Fixture Release Bundle",
        "generator": {"commands": ["fixtures/generate.py"]},
    }
    release_data.update(release or {})
    build_data = {
        "store_id": store_id,
        "layout": "dense",
        "completion_state": "observed_only",
        "build": {"command": "build-dense-vcf", "options": {}},
        "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
    }
    (root / "release.yaml").write_text(yaml.safe_dump(release_data), encoding="utf-8")
    (root / "build.yaml").write_text(yaml.safe_dump(build_data), encoding="utf-8")
    write_manifest(root, rows)
    if validation:
        admitted = sum(1 for row in rows if row.get("exclude_from_build") != "true")
        observed: dict[str, Any] = {"n_variants": 5000, "n_associations": None}
        if n_analyses != "absent":
            observed["n_analyses"] = admitted if n_analyses == "match" else n_analyses
        observed.update(observed_extra or {})
        (root / "validation.yaml").write_text(yaml.safe_dump({
            "status": "passed",
            "validated_at": "2026-10-01T00:00:00Z",
            "build_environment": {"opengwasdb_version": "0.3.0"},
            "observed": observed,
        }), encoding="utf-8")


def write_manifest(root: Path, rows: list[dict[str, str]]) -> None:
    columns = list(REQUIRED_COLUMNS)
    for row in rows:
        columns += [name for name in row if name not in columns]
    lines = ["\t".join(columns)]
    lines += ["\t".join(row.get(name, "") for name in columns) for row in rows]
    (root / "analyses.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def query(artifact_root: Path, sql: str, *args: Any) -> list[tuple]:
    connection = sqlite3.connect(artifact_root / "catalog" / "current" / "catalog.sqlite")
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


def snapshot(root: Path) -> list[tuple[str, int, int]]:
    """Every path under `root` with its mtime and size: what a no-op leaves alone."""
    found = []
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        found.append((str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size))
    return found


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="analysis_catalog_"))
        self.registry = self.tmp / "stores"
        self.registry.mkdir()
        self.root = self.tmp / "artifact-root"
        self.root.mkdir()

    def tearDown(self) -> None:
        for path in self.tmp.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self) -> catalog_build.BuildResult:
        return build_catalog(self.registry, self.root)

    def versions(self) -> list[str]:
        return sorted(p.name for p in (self.root / "catalog" / "versions").iterdir())

    def current(self) -> str:
        return os.readlink(self.root / "catalog" / "current")


class ServingPolicyTest(unittest.TestCase):
    """One fixture bundle per serving-policy case, built once and only read."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="analysis_catalog_policy_"))
        registry, cls.root = cls.tmp / "stores", cls.tmp / "root"
        registry.mkdir()
        cls.root.mkdir()
        write_bundle(registry, "OGS-00001")
        write_bundle(registry, "OGS-00002", release={"access_posture": "controlled"})
        write_bundle(registry, "OGS-00003", release={"trial": True})
        write_bundle(registry, "OGS-00004", release={"access_posture": "embargoed"})
        write_bundle(registry, "OGS-00005", status="candidate", validation=False)
        write_bundle(registry, "OGS-00006", status="accepted", validation=False)
        write_bundle(registry, "OGS-00007", status="built")
        write_bundle(registry, "OGS-00008", status="superseded", release={"superseded_by": "OGS-00001"})
        write_bundle(registry, "OGS-00009", status="withdrawn")
        write_bundle(registry, "OGS-00010", n_analyses=7)
        write_bundle(registry, "OGS-00011", n_analyses="absent")
        write_bundle(registry, "OGS-00012", release={"trial": True, "access_posture": "embargoed"})
        write_bundle(registry, "OGS-00013", status="superseded", release={"trial": True, "superseded_by": "OGS-00001"})
        write_bundle(registry, "OGS-00014", status="withdrawn", release={"access_posture": "embargoed"})
        write_bundle(registry, "OGS-00015", rows=[analysis_row("A1", sample_size="lots")])
        write_bundle(registry, "OGS-00016")
        (registry / "OGS-00016" / "release.yaml").write_text("store_id: OGS-00016\n", encoding="utf-8")
        write_bundle(registry, "OGS-00017", release={"access_posture": None})
        (registry / "by-label").mkdir()
        build_catalog(registry, cls.root)
        cls.selection = {
            store: (decision, reason)
            for store, decision, reason in query(cls.root, "SELECT store_id, decision, reason FROM selection")
        }

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def decision(self, store_id: str) -> tuple[str, str]:
        return self.selection[store_id]

    def test_every_registered_store_release_has_one_decision(self) -> None:
        self.assertEqual(sorted(self.selection), [f"OGS-{n:05d}" for n in range(1, 18)])

    def test_a_validated_public_store_is_listed(self) -> None:
        self.assertEqual(
            self.decision("OGS-00001"),
            ("listed", "Release Status is validated, Access Posture is public"),
        )

    def test_a_controlled_store_is_listed_with_its_access_posture(self) -> None:
        self.assertEqual(
            self.decision("OGS-00002"),
            ("listed", "Release Status is validated, Access Posture is controlled"),
        )
        self.assertEqual(
            query(self.root, "SELECT store_id, access_posture FROM stores WHERE status = 'validated' ORDER BY store_id"),
            [("OGS-00001", "public"), ("OGS-00002", "controlled")],
        )

    def test_a_trial_store_release_is_excluded(self) -> None:
        self.assertEqual(self.decision("OGS-00003"), ("excluded", "Trial Store Release"))

    def test_an_embargoed_store_is_excluded(self) -> None:
        self.assertEqual(self.decision("OGS-00004"), ("excluded", "Access Posture is embargoed"))

    def test_a_store_not_yet_validated_is_excluded(self) -> None:
        self.assertEqual(self.decision("OGS-00005"), ("excluded", "Release Status is candidate, not validated"))
        self.assertEqual(self.decision("OGS-00006"), ("excluded", "Release Status is accepted, not validated"))
        self.assertEqual(self.decision("OGS-00007"), ("excluded", "Release Status is built, not validated"))

    def test_a_superseded_or_withdrawn_store_is_a_placeholder(self) -> None:
        self.assertEqual(
            self.decision("OGS-00008"),
            ("placeholder", "Release Status is superseded: kept as placeholder rows, not listed"),
        )
        self.assertEqual(
            self.decision("OGS-00009"),
            ("placeholder", "Release Status is withdrawn: kept as placeholder rows, not listed"),
        )

    def test_a_count_that_disagrees_with_the_validation_record_is_refused(self) -> None:
        self.assertEqual(
            self.decision("OGS-00010"),
            ("refused", "Release Manifest admits 2 Analyses but the Validation Record's observed.n_analyses is 7"),
        )

    def test_a_missing_validation_count_is_refused_not_matched(self) -> None:
        self.assertEqual(
            self.decision("OGS-00011"),
            ("refused", "Validation Record has no integer observed.n_analyses"),
        )

    def test_trial_and_embargo_come_before_any_other_rule(self) -> None:
        self.assertEqual(self.decision("OGS-00012"), ("excluded", "Trial Store Release"))
        self.assertEqual(self.decision("OGS-00013"), ("excluded", "Trial Store Release"))
        self.assertEqual(self.decision("OGS-00014"), ("excluded", "Access Posture is embargoed"))

    def test_a_non_numeric_value_in_a_numeric_column_refuses_the_store(self) -> None:
        self.assertEqual(
            self.decision("OGS-00015"),
            ("refused", "analyses.tsv row 'A1': sample_size 'lots' is not an integer"),
        )

    def test_a_bundle_that_fails_check_is_refused_with_the_errors(self) -> None:
        decision, reason = self.decision("OGS-00016")
        self.assertEqual(decision, "refused")
        self.assertTrue(reason.startswith("bundle.check() failed: "), reason)

    def test_a_listed_store_needs_an_access_posture(self) -> None:
        self.assertEqual(
            self.decision("OGS-00017"),
            ("refused", "Access Posture is None; a listed Store is public or controlled"),
        )

    def test_the_stores_table_holds_listed_and_placeholder_stores(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT store_id, label, status, layout, completion_state, n_analyses, "
                             "n_variants, n_associations, validated_at, opengwasdb_version "
                             "FROM stores ORDER BY store_id"),
            [
                ("OGS-00001", "label-OGS-00001", "validated", "dense", "observed_only", 2, 5000, None,
                 "2026-10-01T00:00:00Z", "0.3.0"),
                ("OGS-00002", "label-OGS-00002", "validated", "dense", "observed_only", 2, 5000, None,
                 "2026-10-01T00:00:00Z", "0.3.0"),
                ("OGS-00008", "label-OGS-00008", "superseded", "dense", "observed_only", 2, 5000, None,
                 "2026-10-01T00:00:00Z", "0.3.0"),
                ("OGS-00009", "label-OGS-00009", "withdrawn", "dense", "observed_only", 2, 5000, None,
                 "2026-10-01T00:00:00Z", "0.3.0"),
            ],
        )

    def test_association_coverage_and_reference_assembly_are_null_until_reported(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT DISTINCT association_coverage, reference_assembly FROM stores "
                             "WHERE status = 'validated'"),
            [(None, None)],
        )

    def test_catalog_meta_records_the_build(self) -> None:
        meta = dict(query(self.root, "SELECT key, value FROM catalog_meta"))
        self.assertEqual(sorted(meta), ["builder_version", "built_at", "registry_commit", "serving_policy", "version"])
        self.assertEqual(meta["version"], os.path.basename(os.readlink(self.root / "catalog" / "current")))
        self.assertIsNone(meta["registry_commit"])
        self.assertIn("embargoed", meta["serving_policy"])


class AnalysesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="analysis_catalog_analyses_"))
        registry, cls.root = cls.tmp / "stores", cls.tmp / "root"
        registry.mkdir()
        cls.root.mkdir()
        write_bundle(registry, "OGS-00001", rows=[
            analysis_row("G1", source_label="Height", analysis_label="Standing height",
                         original_sd="0.5", n_cases="", sample_size="2500"),
            analysis_row("G2", source_label="Weight"),
            analysis_row("G3", exclude_from_build="true"),
            analysis_row("G4", sample_size_kind="case_control", n_cases="400", n_controls="600",
                         sample_size="1000"),
        ])
        write_bundle(registry, "OGS-00002", rows=[analysis_row("X1", tissue="liver", trait_bp="12345")])
        write_bundle(registry, "OGS-00003", status="candidate", validation=False)
        build_catalog(registry, cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_only_admitted_rows_of_listed_stores_are_analyses(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT id, store_id, analysis_id, status FROM analyses ORDER BY id"),
            [
                ("OGS-00001:G1", "OGS-00001", "G1", "validated"),
                ("OGS-00001:G2", "OGS-00001", "G2", "validated"),
                ("OGS-00001:G4", "OGS-00001", "G4", "validated"),
                ("OGS-00002:X1", "OGS-00002", "X1", "validated"),
            ],
        )

    def test_the_label_falls_back_to_the_source_label(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT id, analysis_label, source_label FROM analyses ORDER BY id LIMIT 3"),
            [
                ("OGS-00001:G1", "Standing height", "Height"),
                ("OGS-00001:G2", "Weight", "Weight"),
                ("OGS-00001:G4", None, None),
            ],
        )

    def test_a_missing_column_and_an_empty_cell_are_null(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT id, tissue, trait_bp, n_cases, original_sd FROM analyses ORDER BY id"),
            [
                ("OGS-00001:G1", None, None, None, 0.5),
                ("OGS-00001:G2", None, None, None, None),
                ("OGS-00001:G4", None, None, 400, None),
                ("OGS-00002:X1", "liver", 12345, None, None),
            ],
        )

    def test_numeric_columns_are_typed(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT typeof(sample_size), typeof(n_cases), typeof(n_controls), typeof(original_sd), "
                             "typeof(trait_bp) FROM analyses WHERE id = 'OGS-00001:G4'"),
            [("integer", "integer", "integer", "null", "null")],
        )
        self.assertEqual(
            query(self.root, "SELECT typeof(original_sd), typeof(trait_bp) FROM analyses WHERE id = 'OGS-00001:G1'"),
            [("real", "null")],
        )
        self.assertEqual(
            query(self.root, "SELECT typeof(trait_bp) FROM analyses WHERE id = 'OGS-00002:X1'"),
            [("integer",)],
        )


class VersionsTest(TempDirCase):
    def edit_label(self, label: str) -> None:
        write_bundle(self.registry, "OGS-00001", rows=[analysis_row("A1", source_label=label), analysis_row("A2")])

    def test_an_unchanged_rebuild_writes_nothing(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        first = self.build()
        before = snapshot(self.root)
        second = self.build()
        self.assertEqual(second.version, first.version)
        self.assertFalse(second.published)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.versions(), [first.version])
        self.assertEqual(self.current(), f"versions/{first.version}")

    def test_an_edited_manifest_row_gives_a_new_version_and_the_report_names_it(self) -> None:
        self.edit_label("Height")
        first = self.build()
        self.edit_label("Standing height")
        second = self.build()
        self.assertNotEqual(second.version, first.version)
        self.assertTrue(second.published)
        self.assertEqual(self.versions(), sorted([first.version, second.version]))
        self.assertEqual(self.current(), f"versions/{second.version}")
        self.assertEqual(
            query(self.root, "SELECT source_label FROM analyses WHERE id = 'OGS-00001:A1'"),
            [("Standing height",)],
        )
        report = (self.root / "catalog" / "current" / "report.md").read_text(encoding="utf-8")
        self.assertIn("Analyses added: 0", report)
        self.assertIn("Analyses removed: 0", report)
        self.assertIn("Analyses with changed metadata: 1", report)
        self.assertIn("- OGS-00001:A1 (analysis_label, source_label)", report)

    def test_the_previous_version_stays_readable(self) -> None:
        self.edit_label("Height")
        first = self.build()
        self.edit_label("Standing height")
        self.build()
        connection = sqlite3.connect(self.root / "catalog" / "versions" / first.version / "catalog.sqlite")
        try:
            self.assertEqual(
                connection.execute("SELECT source_label FROM analyses WHERE id = 'OGS-00001:A1'").fetchall(),
                [("Height",)],
            )
        finally:
            connection.close()

    def test_every_change_is_in_changes_tsv_beyond_the_reports_sample(self) -> None:
        write_bundle(self.registry, "OGS-00001", rows=[analysis_row(f"A{n:04d}") for n in range(250)])
        self.build()
        write_bundle(self.registry, "OGS-00001", rows=[
            analysis_row(f"A{n:04d}", source_label="edited") for n in range(250)
        ] + [analysis_row("Z1")])
        self.build()
        version = self.root / "catalog" / "current"
        with open(version / "changes.tsv", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle, delimiter="\t"))
        self.assertEqual(rows[0], ["id", "change"])
        self.assertEqual(len(rows), 252)
        self.assertEqual(rows[1], ["OGS-00001:A0000", "changed"])
        self.assertEqual(rows[-1], ["OGS-00001:Z1", "added"])
        report = (version / "report.md").read_text(encoding="utf-8")
        self.assertIn("Analyses with changed metadata: 250", report)
        self.assertIn("changes.tsv", report)
        self.assertLess(report.count("- OGS-00001:A"), 250)

    def test_an_added_and_a_removed_analysis_are_reported(self) -> None:
        write_bundle(self.registry, "OGS-00001", rows=[analysis_row("A1"), analysis_row("A2")])
        self.build()
        write_bundle(self.registry, "OGS-00001", rows=[analysis_row("A2"), analysis_row("A3")])
        self.build()
        report = (self.root / "catalog" / "current" / "report.md").read_text(encoding="utf-8")
        self.assertIn("Analyses added: 1\n", report)
        self.assertIn("- OGS-00001:A3\n", report)
        self.assertIn("Analyses removed: 1\n", report)
        self.assertIn("- OGS-00001:A1\n", report)

    def test_editing_an_excluded_stores_manifest_gives_a_new_version(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        write_bundle(self.registry, "OGS-00002", status="candidate", validation=False)
        first = self.build()
        write_bundle(self.registry, "OGS-00002", status="candidate", validation=False,
                     rows=[analysis_row("B1")])
        self.assertNotEqual(self.build().version, first.version)

    def test_a_change_to_an_excluded_stores_decision_gives_a_new_version(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        write_bundle(self.registry, "OGS-00002", status="candidate", validation=False)
        first = self.build()
        write_bundle(self.registry, "OGS-00002", status="accepted", validation=False)
        second = self.build()
        self.assertNotEqual(second.version, first.version)
        self.assertEqual(
            query(self.root, "SELECT reason FROM selection WHERE store_id = 'OGS-00002'"),
            [("Release Status is accepted, not validated",)],
        )

    def test_a_rebuild_repoints_current_without_writing_a_version(self) -> None:
        self.edit_label("Height")
        first = self.build()
        self.edit_label("Standing height")
        second = self.build()
        catalog = self.root / "catalog"
        (catalog / "current").unlink()
        (catalog / "current").symlink_to(f"versions/{first.version}")
        self.edit_label("Standing height")
        rebuilt = self.build()
        self.assertEqual(rebuilt.version, second.version)
        self.assertFalse(rebuilt.published)
        self.assertEqual(self.current(), f"versions/{second.version}")
        self.assertEqual(self.versions(), sorted([first.version, second.version]))

    def test_a_sixth_version_prunes_the_oldest(self) -> None:
        built = []
        for n in range(6):
            self.edit_label(f"Label {n}")
            built.append(self.build().version)
        self.assertEqual(self.versions(), sorted(built[1:]))
        self.assertEqual(self.current(), f"versions/{built[5]}")

    def test_pruning_never_deletes_the_version_current_names(self) -> None:
        built = []
        for n in range(3):
            self.edit_label(f"Label {n}")
            built.append(self.build().version)
        (self.root / "catalog" / "current").unlink()
        (self.root / "catalog" / "current").symlink_to(f"versions/{built[0]}")
        catalog_publish.prune(self.root, keep=1)
        self.assertEqual(self.versions(), sorted([built[0], built[2]]))

    def test_a_stale_partial_directory_is_replaced(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        probe = self.build()
        shutil.rmtree(self.root / "catalog")
        stale = self.root / "catalog" / "versions" / f"{probe.version}.partial"
        stale.mkdir(parents=True)
        (stale / "junk").write_text("left by a crashed build")
        self.build()
        self.assertEqual(self.versions(), [probe.version])


class UnrepresentableValuesTest(TempDirCase):
    """A cell the column cannot hold refuses its Store; the build goes on (#323 review)."""

    def refused(self, **cell: str) -> str:
        write_bundle(self.registry, "OGS-00001")
        write_bundle(self.registry, "OGS-00002", rows=[analysis_row("A1", **cell)])
        self.build()
        self.assertEqual(
            query(self.root, "SELECT decision FROM selection WHERE store_id = 'OGS-00001'"),
            [("listed",)],
        )
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM analyses WHERE store_id = 'OGS-00002'"), [(0,)])
        (decision, reason), = query(self.root, "SELECT decision, reason FROM selection WHERE store_id = 'OGS-00002'")
        self.assertEqual(decision, "refused")
        return reason

    def test_a_real_that_underflows_to_zero_is_refused(self) -> None:
        self.assertEqual(
            self.refused(original_sd="1e-999"),
            "analyses.tsv row 'A1': original_sd '1e-999' is not representable as a REAL",
        )

    def test_a_real_that_overflows_is_refused(self) -> None:
        self.assertEqual(
            self.refused(original_sd="1e999"),
            "analyses.tsv row 'A1': original_sd '1e999' is not representable as a REAL",
        )

    def test_a_signalling_nan_is_refused_not_raised(self) -> None:
        self.assertEqual(
            self.refused(original_sd="sNaN"),
            "analyses.tsv row 'A1': original_sd 'sNaN' is not a finite number",
        )

    def test_an_integer_beyond_sqlites_64_bits_is_refused(self) -> None:
        self.assertEqual(
            self.refused(trait_bp="9223372036854775808"),
            "analyses.tsv row 'A1': trait_bp '9223372036854775808' is not an integer SQLite can store",
        )

    def test_nan_is_the_absence_marker_of_the_two_threshold_columns(self) -> None:
        write_bundle(self.registry, "OGS-00001", rows=[
            analysis_row("A1", info_score_threshold="NaN", maf_threshold="NaN"),
            analysis_row("A2", info_score_threshold="NaN", maf_threshold="0"),
        ])
        self.build()
        self.assertEqual(
            query(self.root, "SELECT id, info_score_threshold, maf_threshold, typeof(maf_threshold) "
                             "FROM analyses ORDER BY id"),
            [("OGS-00001:A1", None, None, "null"), ("OGS-00001:A2", None, 0.0, "real")],
        )

    def test_nan_in_any_other_numeric_column_is_still_refused(self) -> None:
        self.assertEqual(
            self.refused(original_sd="NaN"),
            "analyses.tsv row 'A1': original_sd 'NaN' is not a finite number",
        )

    def test_zero_is_still_a_value(self) -> None:
        write_bundle(self.registry, "OGS-00001", rows=[analysis_row("A1", original_sd="0")])
        self.build()
        self.assertEqual(query(self.root, "SELECT original_sd, typeof(original_sd) FROM analyses"), [(0.0, "real")])


class LabelFallbackTest(TempDirCase):
    def test_label_from_source_records_the_fallback(self) -> None:
        write_bundle(self.registry, "OGS-00001", rows=[
            analysis_row("A1", analysis_label="Curated", source_label="Raw"),
            analysis_row("A2", source_label="Raw"),
            analysis_row("A3"),
        ])
        self.build()
        self.assertEqual(
            query(self.root, "SELECT analysis_id, analysis_label, label_from_source FROM analyses ORDER BY id"),
            [("A1", "Curated", 0), ("A2", "Raw", 1), ("A3", None, 0)],
        )


class EmptyCatalogTest(TempDirCase):
    def test_no_listed_store_gives_a_valid_empty_catalog_and_says_why(self) -> None:
        write_bundle(self.registry, "OGS-00001", status="candidate", validation=False)
        write_bundle(self.registry, "OGS-00002", release={"trial": True})
        self.build()
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM analyses"), [(0,)])
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM stores"), [(0,)])
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM selection"), [(2,)])
        report = (self.root / "catalog" / "current" / "report.md").read_text(encoding="utf-8")
        self.assertIn("No Store Release is listed: 2 registered, 0 listed", report)
        self.assertIn("| OGS-00002 | excluded | Trial Store Release |", report)

    def test_an_empty_registry_is_a_valid_empty_catalog(self) -> None:
        self.build()
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM selection"), [(0,)])


class SummaryRenderTest(TempDirCase):
    """The summary report is rendered into the new version before `current` moves (#325).

    The renderer is injected, so these need no Quarto; the real one is exercised by
    `tests/catalog/smoke_summary.py` (`pixi run docs-smoke`).
    """

    def build_with(self, renderer) -> catalog_build.BuildResult:
        return build_catalog(self.registry, self.root, renderer=renderer)

    def test_a_new_version_has_its_summary_before_current_moves(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        seen = []

        def renderer(version_dir: Path) -> None:
            seen.append((version_dir.name, catalog_publish.current_target(self.root)))
            self.assertTrue((version_dir / "catalog.sqlite").is_file())
            (version_dir / "summary.html").write_text("<html>summary</html>", encoding="utf-8")

        result = self.build_with(renderer)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].endswith(".partial"))
        self.assertIsNone(seen[0][1])
        self.assertEqual((self.root / "catalog" / "current" / "summary.html").read_text(), "<html>summary</html>")
        self.assertEqual(self.current(), f"versions/{result.version}")

    def test_a_failed_render_fails_the_rebuild_and_leaves_current(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        first = self.build_with(lambda d: (d / "summary.html").write_text("first"))
        write_bundle(self.registry, "OGS-00001", rows=[analysis_row("A1"), analysis_row("A3")])

        def failing(version_dir: Path) -> None:
            raise catalog_build.CatalogError("quarto exploded")

        with self.assertRaises(catalog_build.CatalogError):
            self.build_with(failing)
        self.assertEqual(self.current(), f"versions/{first.version}")
        self.assertEqual(self.versions(), [first.version])
        # The next run builds the version afresh rather than trusting a leftover.
        second = self.build_with(lambda d: (d / "summary.html").write_text("second"))
        self.assertNotEqual(second.version, first.version)
        self.assertEqual((self.root / "catalog" / "current" / "summary.html").read_text(), "second")

    def test_the_default_renderer_refuses_before_building_when_the_environment_has_no_quarto(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        with mock.patch.dict(os.environ, {"PATH": ""}):
            os.environ.pop("CONDA_PREFIX", None)
            with self.assertRaises(catalog_build.CatalogError) as raised:
                catalog_build.build_catalog(self.registry, self.root)
        self.assertIn("Quarto", str(raised.exception))
        self.assertFalse((self.root / "catalog").exists())

    def test_a_no_op_rebuild_renders_nothing(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        self.build_with(lambda d: (d / "summary.html").write_text("x"))
        calls = []
        again = self.build_with(calls.append)
        self.assertFalse(again.published)
        self.assertEqual(calls, [])


class NeverOpensAStoreTest(TempDirCase):
    def test_an_unreadable_store_directory_does_not_stop_the_build(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        store = self.root / "OGS-00001"
        (store / "store.opengwasdb").mkdir(parents=True)
        (store / "store.opengwasdb" / "data").write_text("x")
        store.chmod(0)
        self.build()
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM analyses"), [(2,)])


class ExportsTest(TempDirCase):
    def test_the_tsv_exports_hold_exactly_the_tables_rows(self) -> None:
        write_bundle(self.registry, "OGS-00001", rows=[
            analysis_row("A1", source_label="Height", original_sd="0.5"),
            analysis_row("A2"),
        ])
        write_bundle(self.registry, "OGS-00002", release={"access_posture": "controlled"})
        self.build()
        version = self.root / "catalog" / "current"
        for table, path, order in (
            ("stores", version / "stores.tsv", "store_id"),
            ("analyses", version / "analyses.tsv.gz", "id"),
        ):
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8", newline="") as handle:
                exported = list(csv.reader(handle, delimiter="\t"))
            connection = sqlite3.connect(version / "catalog.sqlite")
            try:
                cursor = connection.execute(f"SELECT * FROM {table} ORDER BY {order}")
                header = [column[0] for column in cursor.description]
                expected = [["" if v is None else str(v) for v in row] for row in cursor.fetchall()]
            finally:
                connection.close()
            self.assertEqual(exported[0], header)
            self.assertEqual(exported[1:], expected)
            self.assertGreater(len(expected), 0)
        self.assertEqual(
            [row[0] for row in exported[1:]],
            ["OGS-00001:A1", "OGS-00001:A2", "OGS-00002:A1", "OGS-00002:A2"],
        )


class RegistryCommitTest(TempDirCase):
    def git(self, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}
        return subprocess.run(["git", *args], cwd=self.tmp, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_a_clean_git_registry_records_its_commit(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        self.git("init", "-q")
        self.git("add", "stores")
        self.git("commit", "-q", "-m", "registry")
        self.build()
        self.assertEqual(
            query(self.root, "SELECT value FROM catalog_meta WHERE key = 'registry_commit'"),
            [(self.git("rev-parse", "HEAD"),)],
        )

    def test_uncommitted_changes_in_the_registry_refuse_the_build(self) -> None:
        write_bundle(self.registry, "OGS-00001")
        self.git("init", "-q")
        self.git("add", "stores")
        self.git("commit", "-q", "-m", "registry")
        write_bundle(self.registry, "OGS-00002")
        with self.assertRaisesRegex(catalog_build.CatalogError, "uncommitted changes"):
            self.build()
        self.assertFalse((self.root / "catalog").exists())


class FeaturesTest(unittest.TestCase):
    """Retired Stores, search, counts, types and indexes (#324), on one built catalog."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="analysis_catalog_features_"))
        registry, cls.root = cls.tmp / "stores", cls.tmp / "root"
        registry.mkdir()
        cls.root.mkdir()
        write_bundle(registry, "OGS-00001", observed_extra={"association_coverage": "full"}, rows=[
            analysis_row("L1", analysis_label="Standing height", trait_ontology_label="body height",
                         consortium="GIANT", first_author="Yengo", sample_size="5000"),
            analysis_row("L2", analysis_label="Asthma", assigned_ancestry="EAS",
                         stored_effect_scale="log_or", sample_size_kind="case_control",
                         n_cases="10", n_controls="90"),
            analysis_row("L3", analysis_label="Weight", assigned_ancestry="", sample_size="300"),
            analysis_row("L4", analysis_label="Sitting", stored_effect_scale="sd", sample_size_kind="case_control"),
            analysis_row("L5", analysis_label="Odd", stored_effect_scale="", sample_size_kind="total"),
            analysis_row("L6", analysis_label="Hazard", stored_effect_scale="log_hazard",
                         sample_size_kind="case_control", n_cases="5", n_controls="50"),
            analysis_row("L8", analysis_label="Meta", assigned_ancestry="", source_ancestry_label="META"),
            analysis_row("L9", analysis_label="Other label", assigned_ancestry="",
                         source_ancestry_label="European"),
            analysis_row("L10", analysis_label="Effective", sample_size_kind="effective"),
            analysis_row("L11", analysis_label="Assigned wins", assigned_ancestry="EAS",
                         source_ancestry_label="META"),
        ])
        write_bundle(registry, "OGS-00002", release={"access_posture": "controlled"},
                     observed_extra={"association_coverage": "cis_and_signals"}, rows=[
            analysis_row("E1", analysis_label="BRCA2 expression", tissue="liver", trait_chr="13",
                         trait_bp="32000000", assigned_ancestry="AFR"),
            analysis_row("E2", analysis_label="BRCA2 protein", context="SomaScan", trait_chr="13",
                         trait_bp="32100000"),
            analysis_row("E3", analysis_label="Metabolite", trait_chr="1", trait_bp="5"),
            analysis_row("E4", analysis_label="Blank context", context=" ", tissue=""),
        ])
        write_bundle(registry, "OGS-00003", status="superseded",
                     release={"superseded_by": "OGS-00001"}, rows=[
            analysis_row("S1", analysis_label="Old height", consortium="GIANT"),
            analysis_row("S2", analysis_label="Old hidden", exclude_from_build="true"),
        ])
        write_bundle(registry, "OGS-00004", status="withdrawn", rows=[
            analysis_row("W1", analysis_label="Withdrawn thing", first_author="Gone"),
        ])
        write_bundle(registry, "OGS-00005", status="superseded", release={"trial": True, "superseded_by": "OGS-00001"},
                     rows=[analysis_row("T1")])
        write_bundle(registry, "OGS-00006", status="withdrawn", n_analyses=9)
        build_catalog(registry, cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # placeholder rows

    def test_a_superseded_stores_analyses_resolve_with_their_successor(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT a.id, a.status, s.superseded_by, a.listed FROM analyses a "
                             "JOIN stores s USING (store_id) WHERE a.id = 'OGS-00003:S1'"),
            [("OGS-00003:S1", "superseded", "OGS-00001", 0)],
        )
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM analyses WHERE store_id = 'OGS-00003'"), [(1,)])

    def test_a_withdrawn_stores_analyses_resolve_without_a_successor(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT a.status, s.superseded_by, a.listed FROM analyses a "
                             "JOIN stores s USING (store_id) WHERE a.id = 'OGS-00004:W1'"),
            [("withdrawn", None, 0)],
        )

    def test_a_retired_trial_store_and_a_failed_placeholder_are_not_resolvable(self) -> None:
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM analyses WHERE store_id IN ('OGS-00005', 'OGS-00006')"),
                         [(0,)])
        self.assertEqual(
            query(self.root, "SELECT decision FROM selection WHERE store_id IN ('OGS-00005', 'OGS-00006') "
                             "ORDER BY store_id"),
            [("excluded",), ("refused",)],
        )

    def test_a_retired_store_is_not_listed_searchable_or_counted(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT COUNT(*) FROM listed_analyses WHERE store_id IN ('OGS-00003', 'OGS-00004')"),
            [(0,)],
        )
        for term in ("Old", "Withdrawn", "Gone"):
            self.assertEqual(
                query(self.root, "SELECT COUNT(*) FROM analysis_search WHERE analysis_search MATCH ?", term),
                [(0,)],
            )
        self.assertEqual(
            query(self.root, "SELECT COUNT(*) FROM counts WHERE dimension = 'store' "
                             "AND value IN ('OGS-00003', 'OGS-00004')"),
            [(0,)],
        )

    # search

    def test_search_finds_an_analysis_by_each_of_the_four_fields(self) -> None:
        for term, expected in (
            ("Standing", "OGS-00001:L1"),
            ("body", "OGS-00001:L1"),
            ("Yengo", "OGS-00001:L1"),
            ("expression", "OGS-00002:E1"),
        ):
            with self.subTest(term=term):
                self.assertEqual(
                    query(self.root, "SELECT a.id FROM analysis_search s JOIN analyses a ON a.rowid = s.rowid "
                                     "WHERE analysis_search MATCH ?", term),
                    [(expected,)],
                )
        self.assertEqual(
            query(self.root, "SELECT a.id FROM analysis_search s JOIN analyses a ON a.rowid = s.rowid "
                             "WHERE analysis_search MATCH 'consortium:GIANT'"),
            [("OGS-00001:L1",)],
        )

    # counts

    def test_every_count_table_agrees_with_a_group_by_over_listed_analyses(self) -> None:
        dimensions = {
            "store": "a.store_id",
            "ancestry": "a.ancestry_category",
            "tissue": "a.tissue",
            "effect_scale": "a.stored_effect_scale",
            "outcome_type": "a.outcome_type",
            "trait_class": "a.trait_class",
            "coverage": "s.association_coverage",
        }
        for dimension, expression in dimensions.items():
            with self.subTest(dimension=dimension):
                expected = query(
                    self.root,
                    f"SELECT {expression}, COUNT(*) FROM analyses a JOIN stores s USING (store_id) "
                    "WHERE a.status = 'validated' GROUP BY 1 ORDER BY 1",
                )
                got = query(self.root, "SELECT value, n_analyses FROM counts WHERE dimension = ? ORDER BY value",
                            dimension)
                self.assertEqual(got, expected)
                self.assertEqual(sum(n for _, n in got), 14)

    def test_null_is_a_category_of_its_own(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT value, n_analyses FROM counts WHERE dimension = 'ancestry' ORDER BY value"),
            [(None, 2), ("AFR", 1), ("EAS", 2), ("EUR", 8), ("META", 1)],
        )

    def test_meta_is_counted_as_meta_not_unassigned(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT id, ancestry_category FROM analyses WHERE id IN "
                             "('OGS-00001:L8', 'OGS-00001:L9', 'OGS-00001:L11', 'OGS-00001:L3') ORDER BY id"),
            [("OGS-00001:L11", "EAS"), ("OGS-00001:L3", None), ("OGS-00001:L8", "META"), ("OGS-00001:L9", None)],
        )

    # types

    def test_the_analysis_type_columns(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT id, outcome_type, trait_class FROM analyses WHERE store_id IN "
                             "('OGS-00001', 'OGS-00002') ORDER BY store_id, length(analysis_id), analysis_id"),
            [
                ("OGS-00001:L1", "continuous", "phenotype"),
                ("OGS-00001:L2", "binary", "phenotype"),
                ("OGS-00001:L3", "continuous", "phenotype"),
                ("OGS-00001:L4", "binary", "phenotype"),
                ("OGS-00001:L5", None, "phenotype"),
                ("OGS-00001:L6", "binary", "phenotype"),
                ("OGS-00001:L8", "continuous", "phenotype"),
                ("OGS-00001:L9", "continuous", "phenotype"),
                ("OGS-00001:L10", None, "phenotype"),
                ("OGS-00001:L11", "continuous", "phenotype"),
                ("OGS-00002:E1", "continuous", "molecular"),
                ("OGS-00002:E2", "continuous", "molecular"),
                ("OGS-00002:E3", "continuous", "molecular"),
                ("OGS-00002:E4", "continuous", "phenotype"),
            ],
        )

    # indexes

    def plan(self, sql: str, *args: Any) -> str:
        return " | ".join(row[3] for row in query(self.root, "EXPLAIN QUERY PLAN " + sql, *args))

    def test_sorting_and_keyset_paging_use_an_index_on_every_sortable_column(self) -> None:
        for column in SORTABLE_COLUMNS:
            with self.subTest(column=column):
                if column == "id":
                    sort = self.plan("SELECT id FROM listed_analyses ORDER BY id LIMIT 10")
                    keyset = self.plan("SELECT id FROM listed_analyses WHERE id > ? ORDER BY id LIMIT 10", "x")
                else:
                    sort = self.plan(f"SELECT id FROM listed_analyses ORDER BY {column}, id LIMIT 10")
                    keyset = self.plan(
                        f"SELECT id FROM listed_analyses WHERE ({column}, id) > (?, ?) "
                        f"ORDER BY {column}, id LIMIT 10", "x", "y",
                    )
                for plan in (sort, keyset):
                    self.assertRegex(plan, r"USING (COVERING )?INDEX")
                    self.assertNotIn("TEMP B-TREE", plan)
                self.assertIn("SEARCH", keyset)

    def test_filtering_on_the_derived_columns_searches_an_index(self) -> None:
        for column in ("ancestry_category", "outcome_type", "trait_class", "assigned_ancestry"):
            with self.subTest(column=column):
                plan = self.plan(f"SELECT id FROM listed_analyses WHERE {column} = ? ORDER BY id LIMIT 10", "x")
                self.assertRegex(plan, rf"SEARCH analyses USING (COVERING )?INDEX analyses_by_{column} ")
                self.assertNotIn("TEMP B-TREE", plan)

    def test_a_region_lookup_uses_the_chromosome_and_position_index(self) -> None:
        plan = self.plan("SELECT id FROM listed_analyses WHERE trait_chr = ? AND trait_bp BETWEEN ? AND ?",
                         "13", 1, 2)
        self.assertRegex(plan, r"SEARCH analyses USING (COVERING )?INDEX")
        self.assertIn("trait_bp>", plan)

    def test_a_lookup_by_id_uses_the_primary_key(self) -> None:
        self.assertRegex(self.plan("SELECT * FROM analyses WHERE id = ?", "OGS-00003:S1"),
                         r"SEARCH analyses USING INDEX sqlite_autoindex")


class SearchIntegrityTest(unittest.TestCase):
    """The full-text index covers listed rows only, with placeholders interleaved before and between them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="analysis_catalog_search_"))
        registry, cls.root = cls.tmp / "stores", cls.tmp / "root"
        registry.mkdir()
        cls.root.mkdir()
        write_bundle(registry, "OGS-00001", status="withdrawn",
                     rows=[analysis_row("W1", analysis_label="Gone alpha"), analysis_row("W2", analysis_label="Gone beta")])
        write_bundle(registry, "OGS-00002", rows=[analysis_row("A1", analysis_label="Kept alpha")])
        write_bundle(registry, "OGS-00003", status="superseded", release={"superseded_by": "OGS-00004"},
                     rows=[analysis_row("S1", analysis_label="Old alpha")])
        write_bundle(registry, "OGS-00004", rows=[analysis_row("B1", analysis_label="Kept beta")])
        build_catalog(registry, cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_integrity_check_passes(self) -> None:
        query(self.root, "INSERT INTO analysis_search(analysis_search, rank) VALUES('integrity-check', 1)")

    def test_search_returns_only_listed_rows_with_their_own_text(self) -> None:
        self.assertEqual(
            query(self.root, "SELECT a.id, s.analysis_label FROM analysis_search s JOIN analyses a ON a.rowid = s.rowid "
                             "WHERE analysis_search MATCH 'alpha OR beta' ORDER BY a.id"),
            [("OGS-00002:A1", "Kept alpha"), ("OGS-00004:B1", "Kept beta")],
        )

    def test_a_scan_of_the_index_holds_no_placeholder(self) -> None:
        self.assertEqual(query(self.root, "SELECT COUNT(*) FROM analysis_search"), [(2,)])


SORTABLE_COLUMNS = (
    "id", "store_id", "analysis_label", "trait_ontology_label", "assigned_ancestry", "tissue",
    "stored_effect_scale", "sample_size",
)


if __name__ == "__main__":
    unittest.main()
