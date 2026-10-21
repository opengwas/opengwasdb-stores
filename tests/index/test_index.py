#!/usr/bin/env python3
"""Tests for generated registry views (issues #118 and #131).

Verifies the central contracts of ADR 0022, ADR 0023, and ADR 0028:
1. `stores.tsv`, `STORES.md`, bundle summaries, and symlinks generate from bundles alone.
2. `build_command` is derived purely by `plan(bundle)`, never stored or hand-maintained.
3. Observed columns are extracted exclusively from `validation.yaml` in git, never from the artifact root.
4. Strict seam compliance: index reads git, never opening or inspecting artifact roots (proven via tripwires).
5. `docs/store-catalog.md` is deleted and subsumed by the generated views.
6. CI clean tree check: regenerating every view matches committed files with no drift.
"""

from __future__ import annotations

import builtins
import csv
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

from ogstores import bundle, index, paths
from ogstores.bundle import Bundle
from ogstores.index import (
    COLUMNS,
    build_index,
    generate_by_label_symlinks,
    generate_index,
    regenerate_index,
    render_stores_md,
    render_stores_row,
    render_stores_tsv,
)
from ogstores.plan import plan


def create_mock_bundle(
    stores_dir: Path,
    store_id: str,
    *,
    label: str,
    layout: str = "dense",
    completion_state: str = "observed_only",
    status: str = "built",
    derived_from: str | None = None,
    trial: bool = False,
    validation_data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> Bundle:
    """Create a minimal mock Release Bundle in stores_dir/<store_id>."""
    store_dir = stores_dir / store_id
    store_dir.mkdir(parents=True, exist_ok=True)

    rel_dict = {
        "store_id": store_id,
        "label": label,
        "status": status,
        "derived_from": derived_from,
        "created_at": "2026-08-18T08:51:59Z",
        "description": "Mock release",
        "source_snapshot_id": "mock-snap",
        "generator": {"commands": [f"generate.py --config={label}.yaml"]},
    }
    if trial:
        # Absent means not a trial, so only the marked case is written (#321).
        rel_dict["trial"] = True

    bld_dict: dict[str, Any] = {
        "store_id": store_id,
        "layout": layout,
        "completion_state": completion_state,
        "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
    }
    if completion_state == "reference_completed":
        bld_dict["complete"] = {"command": "complete-dense", "options": options or {"ancestry": "EUR"}}
        if derived_from is None:
            # One id, published twice: the build phase comes first (ADR 0037).
            bld_dict["build"] = {
                "command": "build-dense-vcf",
                "options": {"source-assembly": "hg38"},
            }
    else:
        bld_dict["build"] = {"command": "build-dense-vcf", "options": options or {"source-assembly": "hg38"}}

    with open(store_dir / "release.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(rel_dict, f)
    with open(store_dir / "build.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(bld_dict, f)

    (store_dir / "analyses.tsv").write_text("analysis_id\tsource_file\nana1\tfile1.vcf.gz\n")

    if validation_data is not None:
        with open(store_dir / "validation.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump(validation_data, f)

    return bundle.load(store_id, registry_root=stores_dir)


class TestIndexGenerationAndColumns(unittest.TestCase):
    """Test stores.tsv, STORES.md, bundle summary, and by-label generation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.docs_dir = self.td / "docs"
        self.docs_dir.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_canonical_columns_present_in_order(self) -> None:
        """stores.tsv contains exactly the canonical columns in defined order."""
        self.assertEqual(len(COLUMNS), 28)
        expected = (
            "store_id",
            "label",
            "layout",
            "completion_state",
            "status",
            "derived_from",
            "trial",
            "store_uri",
            "created_at",
            "opengwasdb_rev",
            "generator_command",
            "build_command",
            "format_version",
            "n_analyses",
            "n_manifest",
            "n_excluded",
            "n_variants",
            "n_associations",
            "store_bytes",
            "build_elapsed_s",
            "validate_status",
            "first_author",
            "publication_pmid",
            "tissue",
            "context",
            "assigned_ancestry",
            "sample_size",
            "source_url",
        )
        self.assertEqual(COLUMNS, expected)

    def test_build_command_is_rendered_purely_by_plan(self) -> None:
        """build_command is derived from plan(bundle)[0].argv, not stored or hard-maintained."""
        b = create_mock_bundle(
            self.stores_dir,
            "OGS-00010",
            label="custom-options-pilot",
            options={"source-assembly": "hg38", "n-workers": 16, "chunk-variants": 5000},
        )
        row = render_stores_row(b)
        self.assertTrue(row["build_command"].startswith("opengwasdb build-dense-vcf"))
        # The published command names the derived build manifest, not the bundle's
        # audit analyses.tsv (ADR 0025).
        self.assertIn("/data/opengwasdb/stores/OGS-00010/work/analyses.tsv", row["build_command"])
        self.assertNotIn("stores/OGS-00010/analyses.tsv", row["build_command"])
        self.assertIn("--n-workers 16", row["build_command"])
        self.assertIn("--chunk-variants 5000", row["build_command"])
        self.assertIn("--source-assembly hg38", row["build_command"])

    def test_a_malformed_trial_stops_the_master_list(self) -> None:
        """The index does not gate on `check()`, so a bad marker must not render as `false` (#321)."""
        make = create_mock_bundle(self.stores_dir, "OGS-00021", label="quoted-trial")
        path = make.root / "release.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("status: built", 'status: built\ntrial: "yes"'),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError) as caught:
            render_stores_row(bundle.load("OGS-00021", registry_root=self.stores_dir))
        self.assertIn("trial", str(caught.exception))
        self.assertIn("OGS-00021", str(caught.exception))

    def test_trial_column_reports_the_release_marker(self) -> None:
        """The master list carries the `trial` fact, not a reading of the label (#321)."""
        trial = create_mock_bundle(
            self.stores_dir, "OGS-00019", label="eqtlgen-pilot-10", trial=True
        )
        full = create_mock_bundle(self.stores_dir, "OGS-00020", label="eqtlgen-full")
        rows = [render_stores_row(b) for b in (trial, full)]
        self.assertEqual([row["trial"] for row in rows], ["true", "false"])

        md = render_stores_md([trial, full], rows)
        header = next(line for line in md.splitlines() if line.startswith("| Store ID"))
        self.assertIn("Trial", header)
        self.assertIn("| `OGS-00019` | eqtlgen-pilot-10 | true |", md)
        self.assertIn("| `OGS-00020` | eqtlgen-full | false |", md)

    def test_configured_artifact_root_renders_build_command_and_store_uri(self) -> None:
        """The master list derives artifact paths from configuration, not the Build Recipe (issue #126)."""
        b = create_mock_bundle(
            self.stores_dir,
            "OGS-00013",
            label="configured-root",
        )
        row = render_stores_row(b, artifact_root="/configured/stores")
        self.assertTrue(
            row["build_command"].startswith("opengwasdb build-dense-vcf"),
            row["build_command"],
        )
        self.assertIn("/configured/stores/OGS-00013/work/analyses.tsv", row["build_command"])
        self.assertEqual(row["store_uri"], "/configured/stores/OGS-00013/store.opengwasdb")

    def test_store_uri_is_pure_function_of_identifier_ignoring_migration_note(self) -> None:
        """Issue #137: store_uri derives from the identifier alone, never a migration note.

        A bundle carrying a top-level `store_uri` or a `migration.previous_store_uri`
        must still publish `<artifact-root>/<store-id>/store.opengwasdb`. The
        migration-note fallback is removed, so the master list cannot quietly
        point at a legacy family-first path from the superseded layout.
        """
        b = create_mock_bundle(
            self.stores_dir,
            "OGS-00015",
            label="migration-fallback",
        )
        rel_path = b.root / "release.yaml"
        rel = yaml.safe_load(rel_path.read_text(encoding="utf-8"))
        rel["store_uri"] = "/data/opengwasdb/legacy-family/releases/pilot/store.opengwasdb"
        rel["migration"] = {
            "previous_store_uri": "/data/opengwasdb/legacy-family/releases/pilot/store.opengwasdb",
            "note": "legacy",
        }
        rel_path.write_text(yaml.safe_dump(rel), encoding="utf-8")
        b = bundle.load("OGS-00015", registry_root=self.stores_dir)

        row = render_stores_row(b, artifact_root="/data/opengwasdb/stores")
        self.assertEqual(
            row["store_uri"],
            "/data/opengwasdb/stores/OGS-00015/store.opengwasdb",
            "store_uri must be the pure function of the identifier, ignoring "
            "any migration note or top-level store_uri (issue #137)",
        )

    def test_observed_columns_and_membership_count_have_distinct_sources(self) -> None:
        """Store measurements use validation; Analysis count uses membership."""
        val_data = {
            "status": "passed",
            "validated_at": "2026-09-14T12:00:00Z",
            "observed": {
                "format_version": "1.0",
                "n_analyses": 20,
                "n_variants": 10000,
                "n_associations": 200000,
                "store_bytes": 5242880,
                "build_elapsed_s": 42.5,
                "validate_status": "passed",
            },
        }
        b_built = create_mock_bundle(
            self.stores_dir,
            "OGS-00011",
            label="built-release",
            status="built",
            validation_data=val_data,
        )
        b_cand = create_mock_bundle(
            self.stores_dir,
            "OGS-00012",
            label="candidate-release",
            status="candidate",
            validation_data=None,  # No validation.yaml
        )

        row_built = render_stores_row(b_built)
        self.assertEqual(row_built["format_version"], "1.0")
        self.assertEqual(row_built["n_analyses"], "1")
        self.assertEqual(row_built["n_variants"], "10000")
        self.assertEqual(row_built["n_associations"], "200000")
        self.assertEqual(row_built["store_bytes"], "5242880")
        self.assertEqual(row_built["build_elapsed_s"], "42.5")
        self.assertEqual(row_built["validate_status"], "passed")

        row_cand = render_stores_row(b_cand)
        self.assertEqual(row_cand["format_version"], "")
        self.assertEqual(row_cand["n_analyses"], "1")
        self.assertEqual(row_cand["n_variants"], "")
        self.assertEqual(row_cand["n_associations"], "")
        self.assertEqual(row_cand["store_bytes"], "")
        self.assertEqual(row_cand["build_elapsed_s"], "")
        self.assertEqual(row_cand["validate_status"], "")

    def test_excluded_rows_are_not_counted_as_build_membership(self) -> None:
        """Issue #217: excluded manifest rows stay visible, never counted as Analyses."""
        b = create_mock_bundle(self.stores_dir, "OGS-00016", label="excluded-rows")
        (b.root / "analyses.tsv").write_text(
            "analysis_id\tsource_file\texclude_from_build\n"
            "keep\tf1\tfalse\n"
            "drop\tf2\ttrue\n",
            encoding="utf-8",
        )
        b = bundle.load("OGS-00016", registry_root=self.stores_dir)
        row = render_stores_row(b)
        self.assertEqual(row["n_manifest"], "2")
        self.assertEqual(row["n_analyses"], "1")
        self.assertEqual(row["n_excluded"], "1")
        md = render_stores_md([b], [row])
        self.assertIn("Analyses (admitted)", md)
        # The admitted and excluded counts are separate cells.
        self.assertIn("| 1 | 1 |", md)

    def test_stores_md_table_rows_have_matching_column_counts(self) -> None:
        """Issue #217: the new Excluded column must not desynchronise the table."""
        b1 = create_mock_bundle(self.stores_dir, "OGS-00017", label="wide-1")
        b2 = create_mock_bundle(self.stores_dir, "OGS-00018", label="wide-2")
        rows = [render_stores_row(b) for b in (b1, b2)]
        md = render_stores_md([b1, b2], rows)
        main_section = md.split("\n## ")[0]
        main_rows = [line for line in main_section.splitlines() if line.startswith("|")]
        counts = {line.count("|") for line in main_rows}
        self.assertEqual(len(counts), 1, f"inconsistent STORES.md columns: {counts}")
        header = next(line for line in main_rows if line.startswith("| Store ID"))
        self.assertIn("Excluded", header)

    def test_observed_columns_ignore_legacy_top_level_values(self) -> None:
        """Issue #135: observed columns come only from the register-written `observed` block.

        A pre-seam record can carry `n_analyses`, `format_version` and friends
        at the top level with no `observed` block. After the migration every
        record has the block, so the indexer has no compatibility fallback:
        these top-level values must be ignored rather than published as an
        observed measurement.
        """
        val_data = {
            "status": "passed",
            "format_version": "0.1",
            "n_analyses": 99,
            "n_variants": 999,
            "n_associations": 9999,
            "store_bytes": 12345,
            "build_elapsed_s": 1.5,
        }
        b = create_mock_bundle(
            self.stores_dir,
            "OGS-00014",
            label="legacy-top-level",
            validation_data=val_data,
        )

        row = render_stores_row(b)
        self.assertEqual(row["format_version"], "")
        self.assertEqual(
            row["n_analyses"],
            "1",
            "membership-derived Analysis count must ignore the legacy observed value",
        )
        self.assertEqual(row["n_variants"], "")
        self.assertEqual(row["n_associations"], "")
        self.assertEqual(row["store_bytes"], "")
        self.assertEqual(row["build_elapsed_s"], "")
        # The record's own status is still the published verdict (issue #124).
        self.assertEqual(row["validate_status"], "passed")

    def test_validate_status_is_record_status_not_a_per_check_entry(self) -> None:
        """Issue #124: an overall failure must survive a passing per-check entry.

        A pre-seam Validation Record can carry `status: failed` alongside a
        passing `checks.store` (and a stale `observed.validate_status`). The
        record's own status is the release-level verdict and must win; no
        current Release Bundle exercises this shape, so it is covered
        synthetically. Publishing `passed` here is the worst outcome the
        project can produce (CONTRIBUTING).
        """
        val_data = {
            "status": "failed",
            "validated_at": "2026-09-14T12:00:00Z",
            "observed": {"validate_status": "passed"},
            "checks": {"store": "passed", "files": "passed"},
        }
        b = create_mock_bundle(
            self.stores_dir,
            "OGS-00013",
            label="contradictory-record",
            status="built",
            validation_data=val_data,
        )

        row = render_stores_row(b)
        self.assertEqual(
            row["validate_status"],
            "failed",
            "the Validation Record's own status must override a passing "
            "per-check entry (issue #124)",
        )

    def test_stores_tsv_and_md_and_by_label_generation_end_to_end(self) -> None:
        """generate_index creates every committed view and removes the old catalogue."""
        # Create mock store-catalog.md in docs/
        store_cat_p = self.docs_dir / "store-catalog.md"
        store_cat_p.write_text("# Stale Store Catalog\n")
        self.assertTrue(store_cat_p.is_file())

        b1 = create_mock_bundle(self.stores_dir, "OGS-00001", label="pilot-1")
        b2 = create_mock_bundle(self.stores_dir, "OGS-00002", label="pilot-2")

        mock_artifact_root = self.td / "artifacts"
        mock_artifact_root.mkdir()

        tsv_p, md_p = generate_index(
            registry_root=self.stores_dir,
            repo_root=self.td,
            artifact_root=mock_artifact_root,
        )

        self.assertTrue(tsv_p.is_file())
        self.assertTrue(md_p.is_file())
        self.assertTrue((b1.root / "summary.yaml").is_file())
        self.assertTrue((b2.root / "summary.yaml").is_file())
        self.assertEqual(
            yaml.safe_load((b1.root / "summary.yaml").read_text(encoding="utf-8")),
            bundle.summarise(b1),
        )

        # Assert store-catalog.md was deleted
        self.assertFalse(store_cat_p.exists(), "docs/store-catalog.md must be deleted by generate_index")

        # Verify stores.tsv structure and rows
        with open(tsv_p, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            self.assertEqual(tuple(reader.fieldnames or ()), COLUMNS)
            rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["store_id"], "OGS-00001")
            self.assertEqual(rows[1]["store_id"], "OGS-00002")

        # Verify STORES.md content
        md_text = md_p.read_text(encoding="utf-8")
        self.assertIn("# OpenGWASDB Store Releases", md_text)
        self.assertIn("| `OGS-00001` | pilot-1 | false | dense |", md_text)
        self.assertIn("| `OGS-00002` | pilot-2 | false | dense |", md_text)
        self.assertIn("## Derived membership summaries", md_text)

        # Verify stores/by-label/ symlinks
        by_label_dir = self.stores_dir / "by-label"
        self.assertTrue(by_label_dir.is_dir())
        link1 = by_label_dir / "pilot-1"
        link2 = by_label_dir / "pilot-2"
        self.assertTrue(link1.is_symlink())
        self.assertTrue(link2.is_symlink())
        self.assertEqual(os.readlink(link1), "../OGS-00001")
        self.assertEqual(os.readlink(link2), "../OGS-00002")

        # Verify artifact_root/by-label/ symlinks
        art_by_label_dir = mock_artifact_root / "by-label"
        self.assertTrue(art_by_label_dir.is_dir())
        art_link1 = art_by_label_dir / "pilot-1"
        self.assertTrue(art_link1.is_symlink())
        self.assertEqual(os.readlink(art_link1), "../OGS-00001")


# The `observed` block a register-written record carries. Measurements belong
# to the Store the publication published, so the same bundle renders different
# numbers before and after the single-id swap (issue #307, ADR 0037).
OBSERVED_ONLY_MEASUREMENTS: dict[str, Any] = {
    "format_version": "0.2.0",
    "n_analyses": 2,
    "n_variants": 100,
    "n_associations": 180,
    "store_bytes": 4096,
    "build_elapsed_s": 12.5,
    "validate_status": "passed",
}
REFERENCE_COMPLETED_MEASUREMENTS: dict[str, Any] = {
    "format_version": "0.2.0",
    "n_analyses": 2,
    "n_variants": 250,
    "n_associations": 500,
    "store_bytes": 8192,
    "build_elapsed_s": 40.5,
    "validate_status": "passed",
}


def validation_record(observed: dict[str, Any]) -> dict[str, Any]:
    return {"status": "passed", "observed": dict(observed)}


class TestPublishedCompletionState(unittest.TestCase):
    """The master list reports the Store users can query, not the bundle's declaration (#307).

    Under single-id completion a bundle declares `reference_completed` while
    its Observed-Only Store is what is published, for as long as completion
    runs. The row's `completion_state` and its measurements therefore come from
    the Validation Record, which `register` writes at publication and which
    names the completion state and measurements of the Store it published. A
    record written before that field existed falls back to the declaration, so
    two-id and observed-only rows render exactly as before.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()

    def single_id_bundle(self, observed: dict[str, Any]) -> Bundle:
        """One id, published twice: it declares both phases and is not a two-id child."""
        return create_mock_bundle(
            self.stores_dir,
            "OGS-00041",
            label="single-id",
            completion_state="reference_completed",
            validation_data=validation_record(observed),
        )

    def test_the_interim_row_reports_the_store_users_can_query(self) -> None:
        """Declared reference-completed, published Observed-Only: the row says both."""
        b = self.single_id_bundle(
            dict(OBSERVED_ONLY_MEASUREMENTS, completion_state="observed_only")
        )
        row = render_stores_row(b, artifact_root="/art")

        self.assertEqual(row["completion_state"], "observed_only")
        self.assertEqual(row["n_variants"], "100", "the Observed-Only Store's variants")
        self.assertEqual(row["n_associations"], "180")
        self.assertEqual(row["store_bytes"], "4096")
        self.assertEqual(row["build_elapsed_s"], "12.5")

        md = render_stores_md([b], [row])
        self.assertIn(
            "| `OGS-00041` | single-id | false | dense | observed_only (completion pending) |",
            md,
            "the row must show that a completion is on its way",
        )
        # stores.tsv stays machine-readable: the column holds the published
        # state alone, with no marker text in it.
        tsv_cells = render_stores_tsv([b]).splitlines()[1].split("\t")
        self.assertEqual(tsv_cells[COLUMNS.index("completion_state")], "observed_only")

    def test_after_the_swap_the_row_reports_the_completed_store(self) -> None:
        """The completion phase's record replaces both the state and the measurements."""
        b = self.single_id_bundle(
            dict(REFERENCE_COMPLETED_MEASUREMENTS, completion_state="reference_completed")
        )
        row = render_stores_row(b, artifact_root="/art")

        self.assertEqual(row["completion_state"], "reference_completed")
        self.assertEqual(row["n_variants"], "250", "the completed Store's variants")
        self.assertEqual(row["n_associations"], "500")
        self.assertEqual(row["store_bytes"], "8192")
        md = render_stores_md([b], [row])
        self.assertNotIn("completion pending", md)

    def test_the_build_command_is_the_build_phases_first_step(self) -> None:
        """A single-id row keeps naming the command that builds the Store (ADR 0037)."""
        b = self.single_id_bundle(
            dict(OBSERVED_ONLY_MEASUREMENTS, completion_state="observed_only")
        )
        row = render_stores_row(b, artifact_root="/art")
        self.assertTrue(
            row["build_command"].startswith("opengwasdb build-dense-vcf"),
            row["build_command"],
        )
        self.assertIn("/art/OGS-00041/work/analyses.tsv", row["build_command"])

    def test_an_undocumented_published_state_is_refused(self) -> None:
        """A record's state is published only when it is one `register` documents.

        Falling back to the declaration would hide a new writer's bad value
        behind a plausible one, which is the wrong answer this project refuses.
        """
        b = self.single_id_bundle(
            dict(OBSERVED_ONLY_MEASUREMENTS, completion_state="reference_pending")
        )
        with self.assertRaises(ValueError) as ctx:
            render_stores_row(b, artifact_root="/art")
        self.assertIn("observed.completion_state", str(ctx.exception))
        self.assertIn("reference_pending", str(ctx.exception))

    def test_a_legacy_single_id_record_reports_the_observed_only_store(self) -> None:
        """An amended pre-#307 bundle is still published Observed-Only (ADR 0037).

        ADR 0037 lets an accepted Observed-Only bundle gain a `complete` block,
        so a single-id bundle can declare `reference_completed` while its
        Validation Record predates the field that names the published Store.
        Reporting the declaration would claim a completed Store that does not
        exist; the record's own publication says what users can query.
        """
        legacy = create_mock_bundle(
            self.stores_dir,
            "OGS-00044",
            label="legacy-single-id",
            completion_state="reference_completed",
            validation_data=validation_record(OBSERVED_ONLY_MEASUREMENTS),
        )
        self.assertNotIn("completion_state", OBSERVED_ONLY_MEASUREMENTS)
        self.assertTrue(legacy.completes_own_store)

        row = render_stores_row(legacy, artifact_root="/art")
        self.assertEqual(row["completion_state"], "observed_only")
        self.assertEqual(row["n_variants"], "100", "the Observed-Only Store's variants")
        md = render_stores_md([legacy], [row])
        self.assertIn(
            "| `OGS-00044` | legacy-single-id | false | dense | observed_only (completion pending) |",
            md,
        )
        tsv_cells = render_stores_tsv([legacy], artifact_root="/art").splitlines()[1].split("\t")
        self.assertEqual(tsv_cells[COLUMNS.index("completion_state")], "observed_only")

    def test_an_unpublished_single_id_row_keeps_the_declaration(self) -> None:
        """With no publication recorded there is no Store to report, so nothing is inferred."""
        unpublished = create_mock_bundle(
            self.stores_dir,
            "OGS-00045",
            label="unpublished-single-id",
            completion_state="reference_completed",
        )
        row = render_stores_row(unpublished, artifact_root="/art")
        self.assertEqual(row["completion_state"], "reference_completed")
        self.assertNotIn("completion pending", render_stores_md([unpublished], [row]))

    def test_a_two_id_child_cannot_be_completion_pending(self) -> None:
        """Only a release that completes its own Store paints a Store Observed-Only.

        A two-id child publishes only its completed Store, so a record claiming
        its published Store is Observed-Only contradicts the bundle. It is
        refused rather than labelled pending, which is how readiness reports the
        same pair as a failure.
        """
        child = create_mock_bundle(
            self.stores_dir,
            "OGS-00046",
            label="contradictory-child",
            completion_state="reference_completed",
            derived_from="OGS-00042",
            validation_data=validation_record(
                dict(OBSERVED_ONLY_MEASUREMENTS, completion_state="observed_only")
            ),
        )
        self.assertFalse(child.completes_own_store)
        with self.assertRaises(ValueError) as ctx:
            render_stores_row(child, artifact_root="/art")
        self.assertIn("observed_only", str(ctx.exception))
        self.assertIn("reference_completed", str(ctx.exception))

    def test_a_legacy_two_id_row_renders_exactly_as_before(self) -> None:
        """A record written before #307 carries no published state; the row is unchanged."""
        self.assertNotIn("completion_state", OBSERVED_ONLY_MEASUREMENTS)
        parent = create_mock_bundle(
            self.stores_dir,
            "OGS-00042",
            label="legacy-parent",
            validation_data=validation_record(OBSERVED_ONLY_MEASUREMENTS),
        )
        child = create_mock_bundle(
            self.stores_dir,
            "OGS-00043",
            label="legacy-child",
            completion_state="reference_completed",
            derived_from="OGS-00042",
            validation_data=validation_record(OBSERVED_ONLY_MEASUREMENTS),
        )
        rows = [render_stores_row(b, artifact_root="/art") for b in (parent, child)]

        # Byte-for-byte the rows this repository rendered before #307.
        self.assertEqual(
            render_stores_tsv([parent, child], artifact_root="/art"),
            "\n".join(
                [
                    "\t".join(COLUMNS),
                    "OGS-00042\tlegacy-parent\tdense\tobserved_only\tbuilt\t\t"
                    "false\t/art/OGS-00042/store.opengwasdb\t"
                    "2026-08-18T08:51:59Z\t\t"
                    "generate.py --config=legacy-parent.yaml\t"
                    "opengwasdb build-dense-vcf /art/OGS-00042/work/analyses.tsv "
                    "/art/OGS-00042/store.opengwasdb --store-id OGS-00042 "
                    "--release-id OGS-00042 --source-assembly hg38\t"
                    "0.2.0\t1\t1\t0\t100\t180\t4096\t12.5\tpassed\t"
                    "NA\tNA\tNA\tNA\tNA\tNA\tNA",
                    "OGS-00043\tlegacy-child\tdense\treference_completed\tbuilt\t"
                    "OGS-00042\tfalse\t/art/OGS-00043/store.opengwasdb\t"
                    "2026-08-18T08:51:59Z\t\tgenerate.py --config=legacy-child.yaml\t"
                    "opengwasdb complete-dense /art/OGS-00042/store.opengwasdb "
                    "/art/OGS-00043/store.opengwasdb --release-id OGS-00043 "
                    "--ancestry EUR\t"
                    "0.2.0\t1\t1\t0\t100\t180\t4096\t12.5\tpassed\t"
                    "NA\tNA\tNA\tNA\tNA\tNA\tNA",
                    "",
                ]
            ),
        )
        md = render_stores_md([parent, child], rows)
        self.assertIn(
            "| `OGS-00042` | legacy-parent | false | dense | observed_only | built | "
            "0.2.0 | 1 | 0 | 100 | 180 | passed |",
            md,
        )
        self.assertIn(
            "| `OGS-00043` | legacy-child | false | dense | reference_completed | built | "
            "0.2.0 | 1 | 0 | 100 | 180 | passed |",
            md,
        )
        self.assertNotIn("completion pending", md)


class TestIndexStrictSeamAndTripwires(unittest.TestCase):
    """Verify index reads git and never inspects or opens artifact root / Store files (ADR 0023)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_tripwire_index_opens_no_artifact_root_files_or_store(self) -> None:
        """Strict seam proof (ADR 0023): index reads git only, opening zero artifact files."""
        create_mock_bundle(self.stores_dir, "OGS-00021", label="seam-pilot")

        artifact_root = self.td / "fake_data_opengwasdb"
        artifact_root.mkdir()
        store_file = artifact_root / "OGS-00021" / "store.opengwasdb"
        store_file.mkdir(parents=True)
        (store_file / "manifest.json").write_text('{"store_id": "illegal"}')

        real_open = builtins.open
        real_read_bytes = Path.read_bytes
        real_read_text = Path.read_text
        real_path_open = Path.open
        illegal_opens: list[str] = []

        def check_path(file: Any) -> None:
            path_str = str(file)
            if str(artifact_root) in path_str and not path_str.endswith("by-label"):
                illegal_opens.append(path_str)
                raise AssertionError(f"Tripwire: index illegally accessed artifact file {path_str}")

        def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            check_path(file)
            return real_open(file, *args, **kwargs)

        def guarded_read_bytes(self_path: Path, *args: Any, **kwargs: Any) -> bytes:
            check_path(self_path)
            return real_read_bytes(self_path, *args, **kwargs)

        def guarded_read_text(self_path: Path, *args: Any, **kwargs: Any) -> str:
            check_path(self_path)
            return real_read_text(self_path, *args, **kwargs)

        def guarded_path_open(self_path: Path, *args: Any, **kwargs: Any) -> Any:
            check_path(self_path)
            return real_path_open(self_path, *args, **kwargs)

        try:
            builtins.open = guarded_open  # type: ignore[assignment]
            Path.read_bytes = guarded_read_bytes  # type: ignore[assignment]
            Path.read_text = guarded_read_text  # type: ignore[assignment]
            Path.open = guarded_path_open  # type: ignore[assignment]
            generate_index(
                registry_root=self.stores_dir,
                repo_root=self.td,
                artifact_root=artifact_root,
            )
        finally:
            builtins.open = real_open  # type: ignore[assignment]
            Path.read_bytes = real_read_bytes  # type: ignore[assignment]
            Path.read_text = real_read_text  # type: ignore[assignment]
            Path.open = real_path_open  # type: ignore[assignment]

        self.assertEqual(illegal_opens, [], "index opened artifact files violating ADR 0023")


class TestRepoIndexCleanTree(unittest.TestCase):
    """Verify every generated index view matches the committed files."""

    def test_the_committed_master_list_marks_exactly_the_trial_store_releases(
        self,
    ) -> None:
        """`trial` is a column of the committed list, true for the nine and nobody else (#321)."""
        rows = list(
            csv.DictReader(
                (REPO_ROOT / "stores.tsv").read_text(encoding="utf-8").splitlines(),
                delimiter="\t",
            )
        )
        self.assertIn("trial", rows[0])
        self.assertEqual(
            [row["store_id"] for row in rows if row["trial"] == "true"],
            [
                "OGS-00001",
                "OGS-00002",
                "OGS-00003",
                "OGS-00004",
                "OGS-00005",
                "OGS-00007",
                "OGS-00008",
                "OGS-00022",
                "OGS-00023",
            ],
        )
        self.assertEqual(
            {row["trial"] for row in rows}, {"true", "false"}, "every row says which"
        )
        md = (REPO_ROOT / "STORES.md").read_text(encoding="utf-8")
        header = next(
            line for line in md.splitlines() if line.startswith("| Store ID")
        )
        self.assertIn("Trial", header)
        self.assertIn("| `OGS-00001` | pilot-10 | true |", md)

    def test_current_repo_index_matches_committed_files_with_clean_tree(self) -> None:
        """Regenerating index matches master files, summaries, and symlinks exactly."""
        tsv_path = REPO_ROOT / "stores.tsv"
        md_path = REPO_ROOT / "STORES.md"
        by_label_dir = REPO_ROOT / "stores" / "by-label"

        self.assertTrue(tsv_path.is_file(), "stores.tsv must exist in repository root")
        self.assertTrue(md_path.is_file(), "STORES.md must exist in repository root")
        self.assertTrue(by_label_dir.is_dir(), "stores/by-label/ must exist")

        # Generate index in a temp location from repo's actual stores/
        with tempfile.TemporaryDirectory() as td:
            temp_repo = Path(td)
            gen_tsv, gen_md = generate_index(
                registry_root=REPO_ROOT / "stores",
                repo_root=temp_repo,
            )
            # Compare contents
            self.assertEqual(
                gen_tsv.read_text(encoding="utf-8"),
                tsv_path.read_text(encoding="utf-8"),
                "stores.tsv is dirty or out of date. Run 'pixi run index' to regenerate.",
            )
            self.assertEqual(
                gen_md.read_text(encoding="utf-8"),
                md_path.read_text(encoding="utf-8"),
                "STORES.md is dirty or out of date. Run 'pixi run index' to regenerate.",
            )

        for store_dir in sorted((REPO_ROOT / "stores").glob("OGS-*")):
            if not paths.is_valid_store_id(store_dir.name):
                continue
            summary_path = store_dir / "summary.yaml"
            self.assertTrue(summary_path.is_file(), f"{summary_path} must be committed")
            self.assertEqual(
                yaml.safe_load(summary_path.read_text(encoding="utf-8")),
                bundle.summarise(bundle.load(store_dir.name, registry_root=REPO_ROOT / "stores")),
                f"{summary_path} is dirty or out of date. Run 'pixi run index'.",
            )

        # Verify all bundles in repo stores/ have matching symlinks in stores/by-label/
        repo_stores = [
            d.name for d in (REPO_ROOT / "stores").iterdir()
            if d.is_dir() and paths.is_valid_store_id(d.name) and (d / "release.yaml").is_file()
        ]
        self.assertGreater(len(repo_stores), 0)

        symlink_count = 0
        for sid in repo_stores:
            b = bundle.load(sid, registry_root=REPO_ROOT / "stores")
            if b.label:
                link = by_label_dir / b.label
                self.assertTrue(link.is_symlink(), f"Missing symlink for {b.label} in stores/by-label/")
                self.assertEqual(os.readlink(link), f"../{sid}")
                symlink_count += 1

        all_symlinks = [p for p in by_label_dir.iterdir() if p.is_symlink()]
        self.assertEqual(len(all_symlinks), symlink_count)


if __name__ == "__main__":
    unittest.main()
