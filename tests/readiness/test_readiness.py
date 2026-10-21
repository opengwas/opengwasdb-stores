#!/usr/bin/env python3
"""Tests for Store Release readiness reconciliation (issue #217).

Covers the contracts that keep the readiness view honest:

1. Membership separates intended/admitted/excluded from `analyses.tsv`, and an
   explicit ID-set difference is computed against a historical baseline.
2. Evidence channels distinguish Store validation from candidate-generation
   evidence, and missing/unknown are not passes.
3. Required format-0.2.0 `by_variant` indexes are distinct from optional HM3
   Indexed Variant Subsets.
4. A Reference-Completed child of an incomplete parent is flagged.
5. The only Store access is through the supported `opengwasdb info` seam; the
   reconciler never opens a Store file.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml


from ogstores import readiness
from ogstores.readiness import EvidenceState


def create_mock_bundle(
    stores_dir: Path,
    store_id: str,
    *,
    layout: str = "dense",
    completion_state: str = "observed_only",
    status: str = "built",
    derived_from: str | None = None,
    analyses_rows: list[dict[str, Any]] | None = None,
    validation_data: dict[str, Any] | None = None,
    indexed_subsets: bool = False,
    complete_ancestry: str | None = "EUR",
) -> Any:
    from ogstores import bundle

    store_dir = stores_dir / store_id
    store_dir.mkdir(parents=True, exist_ok=True)
    rel = {
        "store_id": store_id,
        "label": f"label-{store_id}",
        "status": status,
        "derived_from": derived_from,
        "created_at": "2026-08-18T08:51:59Z",
        "description": "mock",
        "source_snapshot_id": "mock-snapshot",
        "generator": {"commands": ["generate.py"]},
    }
    bld: dict[str, Any] = {
        "store_id": store_id,
        "layout": layout,
        "completion_state": completion_state,
        "post": {"top_hits": False, "overview": True, "validate": True},
    }
    if completion_state == "reference_completed":
        bld["complete"] = {
            "command": "complete-dense",
            "options": {"ancestry": complete_ancestry} if complete_ancestry else {},
        }
    else:
        bld["build"] = {"command": "build-dense-vcf", "options": {"source-assembly": "hg38"}}
    if indexed_subsets:
        bld["indexed_subsets"] = [
            {
                "name": "hm3",
                "resource_id": "hapmap3-hg38",
                "variant_list": "/ref/hm3.txt",
                "reference_assembly": "GRCh38",
                "checksum": "0" * 64,
            }
        ]
    (store_dir / "release.yaml").write_text(yaml.safe_dump(rel), encoding="utf-8")
    (store_dir / "build.yaml").write_text(yaml.safe_dump(bld), encoding="utf-8")

    rows = analyses_rows or [{"analysis_id": "a1", "source_file": "f1"}]
    lines = ["analysis_id\tsource_file\texclude_from_build"]
    for row in rows:
        lines.append(
            f"{row['analysis_id']}\t{row.get('source_file', 'f')}\t"
            f"{row.get('exclude_from_build', '')}"
        )
    (store_dir / "analyses.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if validation_data is not None:
        (store_dir / "validation.yaml").write_text(
            yaml.safe_dump(validation_data), encoding="utf-8"
        )
    return bundle.load(store_id, registry_root=stores_dir)


STORE_VALIDATION = {
    "status": "passed",
    "validator": {"name": "opengwasdb validate", "version": "opengwasdb@abc"},
    "build_environment": {"opengwasdb_commit": "a" * 40},
    "warnings": [],
    "errors": [],
}

CANDIDATE_EVIDENCE = {
    "status": "passed_with_warnings",
    "validator": {"name": "generate_candidate.py", "version": None},
    "warnings": ["excluded by policy"],
    "errors": [],
}


class TestMembership(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.stores = Path(self.td.name) / "stores"
        self.stores.mkdir()

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_membership_separates_admitted_and_excluded(self) -> None:
        b = create_mock_bundle(
            self.stores,
            "OGS-00001",
            analyses_rows=[
                {"analysis_id": "keep1"},
                {"analysis_id": "drop1", "exclude_from_build": "true"},
                {"analysis_id": "keep2", "exclude_from_build": "false"},
            ],
        )
        membership = readiness.membership_of(b)
        self.assertEqual(membership.n_manifest, 3)
        self.assertEqual(membership.n_admitted, 2)
        self.assertEqual(membership.n_excluded, 1)
        self.assertEqual(membership.admitted, ("keep1", "keep2"))
        self.assertEqual(membership.excluded, ("drop1",))
        self.assertIsNone(membership.intended)

    def test_malformed_exclusion_fails_loudly(self) -> None:
        b = create_mock_bundle(
            self.stores,
            "OGS-00002",
            analyses_rows=[{"analysis_id": "a", "exclude_from_build": "yes"}],
        )
        with self.assertRaisesRegex(ValueError, "exclude_from_build"):
            readiness.membership_of(b)

    def test_membership_delta_reports_explicit_id_sets(self) -> None:
        b = create_mock_bundle(
            self.stores,
            "OGS-00003",
            analyses_rows=[
                {"analysis_id": "keep"},
                {"analysis_id": "drop", "exclude_from_build": "true"},
            ],
        )
        delta = readiness.membership_delta(
            readiness.membership_of(b), ("keep", "old"), "26fb1ec"
        )
        self.assertEqual(delta.baseline_ref, "26fb1ec")
        self.assertEqual(delta.baseline_admitted, 2)
        self.assertEqual(delta.added, ())
        self.assertEqual(delta.removed, ("old",))


class TestEvidenceChannels(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.stores = Path(self.td.name) / "stores"
        self.stores.mkdir()

    def tearDown(self) -> None:
        self.td.cleanup()

    def _reconcile(self, bundle_obj: Any, ledger: dict, **kwargs: Any):
        by_id = {bundle_obj.store_id: bundle_obj}
        return readiness.reconcile_release(
            bundle_obj, ledger, by_id, set(), **kwargs
        )

    def test_missing_ledger_entry_is_not_a_clean_pass(self) -> None:
        b = create_mock_bundle(self.stores, "OGS-00010", validation_data=STORE_VALIDATION)
        record = self._reconcile(b, {"releases": {}})
        # A committed verdict not proven to match the published artifact is not a
        # current pass; with no host history it is unknown.
        self.assertEqual(record.channels["store_validation"].state, EvidenceState.UNKNOWN)
        self.assertEqual(record.channels["registration"].state, EvidenceState.UNKNOWN)
        self.assertEqual(record.channels["format_runtime"].state, EvidenceState.UNKNOWN)
        self.assertTrue(record.channels["registration"].is_gap)

    def test_candidate_generation_record_is_not_store_validation(self) -> None:
        b = create_mock_bundle(self.stores, "OGS-00011", validation_data=CANDIDATE_EVIDENCE)
        record = self._reconcile(b, {"releases": {}})
        self.assertEqual(record.channels["store_validation"].state, EvidenceState.UNKNOWN)
        self.assertEqual(
            record.channels["committed_validation_record"].state, EvidenceState.MISSING
        )
        self.assertIn("candidate-generation", record.channels["store_validation"].detail)

    def test_required_index_and_optional_hm3_are_distinct(self) -> None:
        ragged = create_mock_bundle(
            self.stores, "OGS-00012", layout="ragged", indexed_subsets=False
        )
        # A published 0.1.0 baseline predates the format-0.2.0 requirement, so a
        # missing by_variant index is not a gap on that artifact (#218).
        record = self._reconcile(ragged, {"releases": {"OGS-00012": {"published_format": "0.1.0"}}})
        self.assertEqual(
            record.channels["required_variant_index"].state, EvidenceState.NOT_APPLICABLE
        )
        record = self._reconcile(
            ragged, {"releases": {"OGS-00012": {"published_format": "0.2.0"}}}
        )
        self.assertEqual(record.channels["required_variant_index"].state, EvidenceState.UNKNOWN)
        self.assertIn("by_variant", record.channels["required_variant_index"].detail)
        self.assertEqual(record.channels["optional_hm3"].state, EvidenceState.NOT_APPLICABLE)

        dense_hm3 = create_mock_bundle(
            self.stores, "OGS-00013", layout="dense", indexed_subsets=True
        )
        record = self._reconcile(
            dense_hm3, {"releases": {"OGS-00013": {"hm3_present": False}}}
        )
        self.assertEqual(record.channels["required_variant_index"].state, EvidenceState.NOT_APPLICABLE)
        self.assertEqual(record.channels["optional_hm3"].state, EvidenceState.MISSING)
        self.assertIn("optional", record.channels["optional_hm3"].detail)

    def test_intended_is_not_the_manifest_count(self) -> None:
        b = create_mock_bundle(
            self.stores,
            "OGS-00020",
            analyses_rows=[{"analysis_id": "a"}, {"analysis_id": "b"}],
        )
        # No authoritative inventory -> unknown, never the manifest count.
        record = self._reconcile(b, {"releases": {}})
        channel = record.channels["intended_membership"]
        self.assertEqual(channel.state, EvidenceState.UNKNOWN)
        self.assertIn("not intended membership", channel.detail)
        self.assertIsNone(record.membership.intended)

        # A preliminary inventory is reported but still not a pass.
        record = self._reconcile(
            b,
            {
                "releases": {
                    "OGS-00020": {
                        "intended_source_inventory": 5,
                        "intended_state": "preliminary",
                    }
                }
            },
        )
        self.assertEqual(record.membership.intended.count, 5)
        self.assertEqual(record.channels["intended_membership"].state, EvidenceState.UNKNOWN)

        # A frozen inventory is a pass.
        record = self._reconcile(
            b,
            {
                "releases": {
                    "OGS-00020": {
                        "intended_source_inventory": 5,
                        "intended_state": "frozen",
                    }
                }
            },
        )
        self.assertEqual(record.channels["intended_membership"].state, EvidenceState.PASS)

    def test_built_id_reconciliation_uses_supported_read(self) -> None:
        b = create_mock_bundle(
            self.stores,
            "OGS-00021",
            layout="dense",
            analyses_rows=[{"analysis_id": "keep"}],
        )
        matching = (("keep",), "opengwasdb v0.3.0")
        record = readiness.reconcile_release(
            b, {"releases": {}}, {"OGS-00021": b}, set(), built_read=matching
        )
        self.assertEqual(record.channels["built_membership"].state, EvidenceState.PASS)
        self.assertEqual(record.built.runtime, "opengwasdb v0.3.0")
        self.assertEqual(record.built_delta.added, ())
        self.assertEqual(record.built_delta.removed, ())

        extra = (("keep", "legacy"), "opengwasdb v0.3.0")
        record = readiness.reconcile_release(
            b, {"releases": {}}, {"OGS-00021": b}, set(), built_read=extra
        )
        self.assertEqual(record.channels["built_membership"].state, EvidenceState.FAIL)
        self.assertEqual(record.built_delta.removed, ("legacy",))
        self.assertEqual(record.built_delta.added, ())
        self.assertEqual(record.built_delta.kind, "built_reconciliation")

    def test_core_reporting_support_surface(self) -> None:
        """The supported public core API this module relies on must exist."""
        support = readiness.core_reporting_support()
        self.assertTrue(support["open_store"])
        self.assertTrue(support["analyses_path"])
        self.assertTrue(support["read_analyses"])
        self.assertIn("opengwasdb", readiness.core_runtime_identity())

    def test_incomplete_parent_flags_derived_child(self) -> None:
        parent = create_mock_bundle(
            self.stores,
            "OGS-00009",
            analyses_rows=[{"analysis_id": "a"}, {"analysis_id": "b"}],
        )
        child = create_mock_bundle(
            self.stores,
            "OGS-00010",
            completion_state="reference_completed",
            derived_from="OGS-00009",
            analyses_rows=[{"analysis_id": "a"}],
        )
        ledger = {
            "releases": {
                "OGS-00009": {
                    "built_analyses": 2,
                    "incomplete_membership": True,
                    "blockers": ["restore missing analyses"],
                }
            }
        }
        by_id = {"OGS-00009": parent, "OGS-00010": child}
        child_record = readiness.reconcile_release(
            child, ledger, by_id, {"OGS-00009"}
        )
        self.assertEqual(child_record.channels["lineage"].state, EvidenceState.FAIL)
        self.assertIn("incomplete", child_record.channels["lineage"].detail)
        parent_record = readiness.reconcile_release(
            parent,
            ledger,
            by_id,
            {"OGS-00009"},
            built_read=(("a", "b"), "opengwasdb v0.3.0"),
        )
        self.assertEqual(parent_record.channels["observed_build"].state, EvidenceState.FAIL)
        self.assertIn("incomplete", parent_record.channels["observed_build"].detail)
        self.assertEqual(parent_record.built.runtime, "opengwasdb v0.3.0")

    def test_format_runtime_pass_and_mismatch(self) -> None:
        b = create_mock_bundle(self.stores, "OGS-00016", layout="dense")
        good = {
            "format_version": "0.2.0",
            "primary_layout": "dense",
            "completion_state": "observed_only",
        }
        record = self._reconcile(b, {"releases": {}}, info=good)
        self.assertEqual(record.channels["format_runtime"].state, EvidenceState.PASS)

        bad = dict(good, primary_layout="ragged")
        record = self._reconcile(b, {"releases": {}}, info=bad)
        self.assertEqual(record.channels["format_runtime"].state, EvidenceState.FAIL)
        self.assertIn("layout", record.channels["format_runtime"].detail)

    def test_unreadable_baseline_is_a_recorded_blocker_not_a_crash(self) -> None:
        b = create_mock_bundle(
            self.stores,
            "OGS-00011",
            layout="hybrid",
            analyses_rows=[{"analysis_id": "keep"}],
        )
        ledger = {
            "releases": {
                "OGS-00011": {"built_analyses": 2, "built_baseline_ref": "deadbeef"}
            }
        }

        def failing_reader(sid: str, ref: str) -> tuple[str, ...]:
            raise ValueError("shallow checkout")

        record = readiness.reconcile_release(
            b,
            ledger,
            {"OGS-00011": b},
            set(),
            baseline_reader=failing_reader,
        )
        self.assertIsNone(record.delta)
        self.assertTrue(any("baseline" in blocker for blocker in record.blockers))
        # Without the baseline the published count (2) is compared with current
        # admitted membership (1), so the mismatch is visible rather than hidden.
        self.assertEqual(record.built.state, EvidenceState.FAIL)

    def test_configured_evidence_is_not_a_verified_pass(self) -> None:
        b = create_mock_bundle(self.stores, "OGS-00030")
        record = self._reconcile(b, {"releases": {}})
        self.assertEqual(
            record.channels["source_acquisition"].state, EvidenceState.UNKNOWN
        )
        record = self._reconcile(
            b, {"releases": {"OGS-00030": {"source_acquisition_verified": True}}}
        )
        self.assertEqual(record.channels["source_acquisition"].state, EvidenceState.PASS)

    def test_completion_configured_is_unknown_and_verified_is_pass(self) -> None:
        parent = create_mock_bundle(
            self.stores, "OGS-00031", analyses_rows=[{"analysis_id": "a"}]
        )
        child = create_mock_bundle(
            self.stores,
            "OGS-00032",
            completion_state="reference_completed",
            derived_from="OGS-00031",
            analyses_rows=[{"analysis_id": "a"}],
        )
        by_id = {"OGS-00031": parent, "OGS-00032": child}
        record = readiness.reconcile_release(child, {"releases": {}}, by_id, set())
        self.assertEqual(
            record.channels["reference_completion"].state, EvidenceState.UNKNOWN
        )
        record = readiness.reconcile_release(
            child,
            {"releases": {"OGS-00032": {"completion_verified": True}}},
            by_id,
            set(),
        )
        self.assertEqual(
            record.channels["reference_completion"].state, EvidenceState.PASS
        )

    def test_store_validation_provenance_is_classified(self) -> None:
        b = create_mock_bundle(
            self.stores, "OGS-00033", validation_data=STORE_VALIDATION
        )
        matched = self._reconcile(
            b, {"releases": {"OGS-00033": {"committed_matches_published": True}}}
        )
        self.assertEqual(matched.channels["store_validation"].state, EvidenceState.PASS)
        mismatched = self._reconcile(
            b,
            {
                "releases": {
                    "OGS-00033": {
                        "committed_matches_published": False,
                        "host_validation": "published Store built 2026-09-18",
                    }
                }
            },
        )
        self.assertEqual(
            mismatched.channels["store_validation"].state, EvidenceState.HISTORICAL
        )
        self.assertIn("different", mismatched.channels["store_validation"].detail)

    def test_incomplete_info_is_unknown(self) -> None:
        b = create_mock_bundle(self.stores, "OGS-00034", layout="dense")
        record = self._reconcile(b, {"releases": {}}, info={})
        self.assertEqual(record.channels["format_runtime"].state, EvidenceState.UNKNOWN)

    def test_format_migration_is_separate_from_baseline_validity(self) -> None:
        b = create_mock_bundle(self.stores, "OGS-00035", layout="ragged")
        record = self._reconcile(
            b, {"releases": {"OGS-00035": {"published_format": "0.1.0"}}}
        )
        self.assertEqual(
            record.channels["required_variant_index"].state, EvidenceState.NOT_APPLICABLE
        )
        self.assertEqual(record.channels["format_migration"].state, EvidenceState.MISSING)
        record = self._reconcile(
            b, {"releases": {"OGS-00035": {"published_format": "0.2.0"}}}
        )
        self.assertEqual(record.channels["format_migration"].state, EvidenceState.PASS)

    def test_ledger_only_built_count_is_historical_not_pass(self) -> None:
        b = create_mock_bundle(
            self.stores, "OGS-00036", analyses_rows=[{"analysis_id": "a"}]
        )
        record = self._reconcile(
            b, {"releases": {"OGS-00036": {"built_analyses": 1}}}
        )
        self.assertEqual(record.built.state, EvidenceState.HISTORICAL)
        self.assertEqual(
            record.channels["built_membership"].state, EvidenceState.HISTORICAL
        )

    def test_release_status_declared_is_labelled_not_operational(self) -> None:
        b = create_mock_bundle(self.stores, "OGS-00037", status="candidate")
        record = self._reconcile(b, {"releases": {}})
        channel = record.channels["release_status"]
        self.assertEqual(channel.state, EvidenceState.PASS)
        self.assertIn("declared", channel.detail)
        self.assertIn("not an operational", channel.detail)
        bad = create_mock_bundle(self.stores, "OGS-00038", status="not_a_status")
        record = self._reconcile(bad, {"releases": {}})
        self.assertEqual(record.channels["release_status"].state, EvidenceState.FAIL)

    def test_warnings_absence_without_validation_is_unknown(self) -> None:
        b = create_mock_bundle(self.stores, "OGS-00039")
        record = self._reconcile(b, {"releases": {}})
        self.assertEqual(record.channels["warnings"].state, EvidenceState.UNKNOWN)
        clean = create_mock_bundle(
            self.stores, "OGS-00040", validation_data=STORE_VALIDATION
        )
        record = self._reconcile(clean, {"releases": {}})
        self.assertEqual(record.channels["warnings"].state, EvidenceState.PASS)
        noisy = create_mock_bundle(
            self.stores,
            "OGS-00041",
            validation_data=dict(STORE_VALIDATION, warnings=["orientation unverified"]),
        )
        record = self._reconcile(noisy, {"releases": {}})
        self.assertEqual(
            record.channels["warnings"].state, EvidenceState.PASS_WITH_WARNINGS
        )

    def test_baseline_delta_and_built_history(self) -> None:
        b = create_mock_bundle(
            self.stores,
            "OGS-00011",
            layout="hybrid",
            analyses_rows=[
                {"analysis_id": "keep"},
                {"analysis_id": "drop", "exclude_from_build": "true"},
            ],
        )
        ledger = {
            "releases": {
                "OGS-00011": {
                    "built_analyses": 2,
                    "built_baseline_ref": "26fb1ec",
                    "baseline_note": "old artifact",
                }
            }
        }
        record = readiness.reconcile_release(
            b,
            ledger,
            {"OGS-00011": b},
            set(),
            baseline_reader=lambda sid, ref: ("keep", "old"),
        )
        self.assertEqual(record.delta.baseline_admitted, 2)
        self.assertEqual(record.delta.removed, ("old",))
        # A recorded historical count that matches its baseline is not a pass:
        # identity is unread, so it is history only.
        self.assertEqual(record.built.analyses, 2)
        self.assertEqual(record.built.state, EvidenceState.HISTORICAL)


class TestSupportedInfoSeam(unittest.TestCase):
    def test_store_info_parses_supported_command_output(self) -> None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            self.assertIn("opengwasdb", argv)
            self.assertIn("info", argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"format_version": "0.2.0"}', stderr=""
            )

        payload = readiness.store_info("/does/not/exist", runner=runner)
        self.assertEqual(payload["format_version"], "0.2.0")

    def test_store_info_fails_loudly_on_bad_exit(self) -> None:
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

        with self.assertRaisesRegex(ValueError, "boom"):
            readiness.store_info("/does/not/exist", runner=runner)


class TestRenderingAndRepoView(unittest.TestCase):
    def test_repo_readiness_view_reconciles_all_registered_releases(self) -> None:
        ledger = readiness.load_ledger()
        releases = readiness.reconcile_registry(ledger=ledger)
        by_id = {r.store_id: r for r in releases}
        self.assertIn("OGS-00011", by_id)
        eleven = by_id["OGS-00011"]
        self.assertEqual(eleven.membership.n_manifest, 4783)
        self.assertEqual(eleven.membership.n_admitted, 3169)
        self.assertEqual(eleven.membership.n_excluded, 1614)
        self.assertIsNotNone(eleven.membership.intended)
        self.assertEqual(eleven.membership.intended.count, 6035)
        # The delta needs the baseline commit; a shallow checkout may not carry
        # it, in which case the tool records a blocker instead of inventing one.
        if eleven.delta is not None:
            self.assertEqual(eleven.delta.baseline_admitted, 3317)
            self.assertEqual(len(eleven.delta.removed), 148)
            self.assertEqual(eleven.delta.added, ())
        else:
            self.assertTrue(
                any("baseline" in blocker for blocker in eleven.blockers),
                "a missing baseline must be a recorded blocker, not silence",
            )
        # Nine is the admitted membership, not the ten manifest rows (#217).
        self.assertEqual(by_id["OGS-00004"].membership.n_admitted, 9)

    def test_markdown_reports_membership_and_delta(self) -> None:
        ledger = readiness.load_ledger()
        releases = readiness.reconcile_registry(ledger=ledger)
        text = readiness.render_readiness_markdown(releases, ledger)
        self.assertIn("Intended", text)
        self.assertIn("Admitted", text)
        self.assertIn("Excluded", text)
        self.assertIn("26fb1ec", text)
        self.assertIn("OGS-00011", text)

    def test_yaml_document_round_trips(self) -> None:
        ledger = readiness.load_ledger()
        releases = readiness.reconcile_registry(ledger=ledger)
        text = readiness.dump_readiness_yaml(releases, ledger)
        parsed = yaml.safe_load(text)
        self.assertEqual(len(parsed["releases"]), len(releases))


if __name__ == "__main__":
    unittest.main()
