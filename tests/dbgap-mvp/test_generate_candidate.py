#!/usr/bin/env python3
"""Tests for the MVP ancestry/META candidate emitter (#221).

Covers the ticket-specific contracts with small fixtures:

1. The metadata partition is exact, disjoint and reconciles the legacy European
   bucket as EUR + META.
2. META is emitted observed-only: no ``derived_from`` and no completion block.
3. A lineage-linked completed child is emitted only for a qualified reference;
   a ``hold`` reference produces a plan, not a bundle.
4. Every emitted candidate is ``status: candidate`` with human acceptance
   explicitly pending and no Store build/validation evidence.
5. Missing or extra resolution rows fail closed.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "resources" / "generators" / "dbgap-mvp"
for extra in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

_spec = importlib.util.spec_from_file_location(
    "dbgap_mvp_generate", SCRIPT_DIR / "generate_candidate.py"
)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
sys.modules["dbgap_mvp_generate"] = gen
_spec.loader.exec_module(gen)
partition = gen.partition_module

# Fixture membership: small, but the shape mirrors the real buckets.
MEMBERS = {
    "EUR": ["eur1", "eur2", "eur3", "eur4", "eur5"],
    "AFR": ["afr1", "afr2", "afr3"],
    "AMR": ["amr1", "amr2"],
    "EAS": ["eas1"],
    "META": ["meta1", "meta2", "meta3", "meta4"],
}
EXPECTED = {bucket: len(ids) for bucket, ids in MEMBERS.items()}

FIXTURE_STORES = {
    "EUR": ("OGS-00012", "OGS-00018", "EUR", "European", "pending", "/fixture/ld"),
    "AFR": ("OGS-00013", "OGS-00019", "AFR", "African", "pending", "/fixture/ld"),
    "AMR": ("OGS-00014", "OGS-00020", "AMR", "Hispanic or Latin American", "hold", None),
    "EAS": ("OGS-00015", "OGS-00021", "EAS", "East Asian", "pending", "/fixture/ld"),
    "META": ("OGS-00017", None, None, "META", "not_applicable", None),
}


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def make_fixture(tmp: Path) -> tuple[Path, Path, Path]:
    mapping_rows = [
        {"analysis_id": analysis_id, "ancestry": ancestry}
        for ancestry, ids in MEMBERS.items()
        for analysis_id in ids
    ]
    mapping_path = tmp / "mapping.tsv"
    write_tsv(mapping_path, ["analysis_id", "ancestry"], mapping_rows)

    resolution_rows = []
    for ancestry, ids in MEMBERS.items():
        for analysis_id in ids:
            resolution_rows.append(
                {
                    "analysis_id": analysis_id,
                    "assigned_ancestry": ancestry if ancestry != "META" else "",
                    "ancestry_assignment_method": "source_fallback",
                    "stored_effect_scale": "sd",
                    "sample_size_kind": "total",
                    "sample_size_scope": "analysis_level",
                    "sample_size": "1000",
                    "original_effect_scale": "sd",
                    "original_sd_method": "source_provided",
                }
            )
    resolution_path = tmp / "resolution.tsv"
    write_tsv(
        resolution_path,
        [
            "analysis_id",
            "assigned_ancestry",
            "ancestry_assignment_method",
            "stored_effect_scale",
            "sample_size_kind",
            "sample_size_scope",
            "sample_size",
            "original_effect_scale",
            "original_sd_method",
        ],
        resolution_rows,
    )

    stores = {}
    for ancestry, (observed, completed, assigned, label_ancestry, qualification, suggested) in FIXTURE_STORES.items():
        stores[ancestry] = {
            "observed_store_id": observed,
            "completed_store_id": completed,
            "candidate_store_key": partition.STORE_KEY_BY_ANCESTRY[ancestry],
            "label": f"mvp-{ancestry.lower()}",
            "assigned_ancestry": assigned,
            "source_ancestry_label": label_ancestry,
            "observed_only": ancestry == "META",
            "intended_members": len(MEMBERS[ancestry]),
            "completion_reference": {
                "resource_id": "fixture-ld" if suggested else None,
                "ancestry": assigned,
                "declared_status": "available" if suggested else "absent",
                "qualification": qualification,
                "activation": "pending" if qualification == "pending" else "blocked",
                "suggested_ld_panel": suggested,
            },
        }
    config = {
        "id_allocation_status": "provisional-draft",
        "owner_ordering_approved": False,
        "intended_bound_total": 15,
        "publication_pmid": "39024449",
        "access_posture": "public",
        "layout": "dense",
        "source_snapshot_id": "fixture-mvp-pending",
        "source_reader_capability": "opengwasdb.gwas-ssf",
        "source_assembly": "GRCh38",
        "generator_commands": ["generate_candidate.py"],
        "partition_expected": EXPECTED,
        "stores": stores,
        "finngen": {"current_store_id": "OGS-00016", "renumbered_to": None},
    }
    config_path = tmp / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return mapping_path, resolution_path, config_path


class TestPartition(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.mapping, self.resolution, self.config_path = make_fixture(self.tmp)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_partition_is_exact_and_disjoint(self) -> None:
        rows = partition.read_mapping(self.mapping)
        grouped = partition.partition_mapping(rows)
        partition.validate_partition(grouped, expected=EXPECTED)
        self.assertEqual(partition.legacy_european_count(grouped), 9)
        all_ids = [analysis_id for bucket in partition.ANCESTRY_ORDER for analysis_id in grouped[bucket]]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_unknown_ancestry_fails(self) -> None:
        bad = self.tmp / "bad.tsv"
        write_tsv(bad, ["analysis_id", "ancestry"], [{"analysis_id": "x", "ancestry": "OCEANIA"}])
        with self.assertRaisesRegex(partition.PartitionError, "unrecognised"):
            partition.partition_mapping(partition.read_mapping(bad))

    def test_count_drift_fails(self) -> None:
        rows = partition.read_mapping(self.mapping)
        grouped = partition.partition_mapping(rows)
        with self.assertRaisesRegex(partition.PartitionError, "expected 4"):
            partition.validate_partition(grouped, expected={**EXPECTED, "EUR": 4})


class TestCandidateEmission(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.mapping, self.resolution, self.config_path = make_fixture(self.tmp)
        self.config = gen.load_config(self.config_path)
        self.out = self.tmp / "out"

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_plan_mode_validates_and_writes_partition_only(self) -> None:
        plan = gen.run_plan(self.config, self.mapping, self.out)
        self.assertEqual(plan["total"], 15)
        self.assertEqual(plan["legacy_european_bucket"], 9)
        self.assertTrue((self.out / "partition.tsv").is_file())
        self.assertFalse((self.out / "OGS-00012").exists())

    def test_emit_produces_pending_candidates(self) -> None:
        result = gen.run_emit(
            self.config,
            self.mapping,
            self.resolution,
            self.out,
            created_at="2026-10-13T00:00:00+00:00",
        )
        self.assertEqual(
            set(result["emitted"]),
            {"EUR", "AFR", "AMR", "EAS", "META", "EUR-completed", "AFR-completed", "EAS-completed"},
        )
        self.assertEqual(sorted(result["checked"]), sorted(result["emitted"].values()))

        # META is observed-only: no parent and no completion recipe.
        meta_release = yaml.safe_load((self.out / "OGS-00017" / "release.yaml").read_text())
        self.assertEqual(meta_release["status"], "candidate")
        self.assertIsNone(meta_release["derived_from"])
        self.assertNotIn("accepted_at", meta_release)
        meta_build = yaml.safe_load((self.out / "OGS-00017" / "build.yaml").read_text())
        self.assertEqual(meta_build["completion_state"], "observed_only")
        self.assertNotIn("complete", meta_build)

        # EUR completed definition is lineage-linked and carries the suggested
        # panel, but its reference stays pending/blocked (never promoted).
        eur_child_release = yaml.safe_load((self.out / "OGS-00018" / "release.yaml").read_text())
        self.assertEqual(eur_child_release["derived_from"], "OGS-00012")
        self.assertEqual(eur_child_release["status"], "candidate")
        eur_child_build = yaml.safe_load((self.out / "OGS-00018" / "build.yaml").read_text())
        self.assertEqual(eur_child_build["completion_state"], "reference_completed")
        self.assertIn("complete", eur_child_build)
        self.assertEqual(
            eur_child_build["complete"]["options"]["ld-panel"], "/fixture/ld"
        )

        # AMR has no suggested reference: a pending definition, not a bundle.
        self.assertFalse((self.out / "OGS-00020").exists())
        amr_plan = yaml.safe_load((self.out / "OGS-00020.completion-plan.yaml").read_text())
        self.assertEqual(amr_plan["status"], "pending")
        self.assertTrue(amr_plan["blocked"])
        self.assertEqual(amr_plan["human_acceptance"], "pending")
        self.assertEqual(amr_plan["reference"]["qualification"], "hold")
        self.assertIsNone(amr_plan["reference"]["resource_id"])

    def test_available_reference_does_not_promote_qualification(self) -> None:
        """A declared `available` reference is not a qualification or science pass."""
        gen.run_emit(
            self.config, self.mapping, self.resolution, self.out, created_at="2026-10-13T00:00:00+00:00"
        )
        validation = yaml.safe_load((self.out / "OGS-00018" / "validation.yaml").read_text())
        reference = validation["acceptance"]["completion_reference"]
        self.assertEqual(reference["declared_status"], "available")
        self.assertEqual(reference["qualification"], "pending")
        self.assertEqual(reference["activation"], "pending")
        self.assertTrue(reference["blocked"])
        # Nothing in the emitted tree may claim the reference is qualified.
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(self.out.rglob("*"))
            if path.is_file()
        )
        self.assertNotIn("qualified", text.replace("not qualified", ""))

    def test_missing_reference_still_emits_pending_definition(self) -> None:
        gen.run_emit(
            self.config, self.mapping, self.resolution, self.out, created_at="2026-10-13T00:00:00+00:00"
        )
        definition = yaml.safe_load((self.out / "OGS-00020.completion-plan.yaml").read_text())
        self.assertEqual(definition["human_acceptance"], "pending")
        self.assertTrue(definition["blocked"])
        self.assertFalse((self.out / "OGS-00020").exists())

    def test_candidate_definitions_are_tracked_and_pending(self) -> None:
        path = gen.write_candidate_definitions(self.config, self.out)
        definitions = yaml.safe_load(path.read_text())
        self.assertEqual(definitions["status"], "provisional-draft")
        self.assertFalse(definitions["owner_ordering_approved"])
        for bucket in definitions["stores"].values():
            self.assertFalse(bucket["observed"]["in_stores"])
            self.assertEqual(bucket["observed"]["acceptance"], "pending")
            if bucket["completed"] is not None:
                self.assertEqual(bucket["completed"]["human_acceptance"], "pending")
                self.assertTrue(bucket["completed"]["blocked"])

    def test_acceptance_is_pending_and_not_verified(self) -> None:
        gen.run_emit(
            self.config, self.mapping, self.resolution, self.out, created_at="2026-10-13T00:00:00+00:00"
        )
        validation = yaml.safe_load((self.out / "OGS-00012" / "validation.yaml").read_text())
        self.assertEqual(validation["status"], "not_run")
        self.assertEqual(validation["acceptance"]["human_acceptance"], "pending")
        self.assertFalse(validation["acceptance"]["membership_accepted"])
        self.assertIn("Candidate", " ".join(validation["warnings"]))

    def test_missing_resolution_member_fails_closed(self) -> None:
        rows = [row for row in gen.read_tsv(self.resolution) if row["analysis_id"] != "eur3"]
        truncated = self.tmp / "truncated.tsv"
        write_tsv(truncated, list(rows[0].keys()), rows)
        with self.assertRaisesRegex(gen.CandidateError, "no resolution row"):
            gen.run_emit(
                self.config,
                self.mapping,
                truncated,
                self.out,
                created_at="2026-10-13T00:00:00+00:00",
            )

    def test_extra_resolution_member_fails_closed(self) -> None:
        rows = gen.read_tsv(self.resolution)
        extra = dict(rows[0], analysis_id="not-in-mapping")
        rows.append(extra)
        widened = self.tmp / "widened.tsv"
        write_tsv(widened, list(rows[0].keys()), rows)
        with self.assertRaisesRegex(gen.CandidateError, "outside the intended membership"):
            gen.run_emit(
                self.config,
                self.mapping,
                widened,
                self.out,
                created_at="2026-10-13T00:00:00+00:00",
            )

    def test_emit_refuses_to_overwrite(self) -> None:
        gen.run_emit(
            self.config, self.mapping, self.resolution, self.out, created_at="2026-10-13T00:00:00+00:00"
        )
        with self.assertRaisesRegex(gen.CandidateError, "refusing to overwrite"):
            gen.run_emit(
                self.config,
                self.mapping,
                self.resolution,
                self.out,
                created_at="2026-10-13T00:00:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
