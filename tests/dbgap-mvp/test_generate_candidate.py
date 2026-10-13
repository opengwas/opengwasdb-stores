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
import json
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

PLANNED_MANIFEST = {
    "EUR": "european.tsv",
    "AFR": "african.tsv",
    "AMR": "amr.tsv",
    "EAS": "eas.tsv",
    "META": "european.tsv",
}

MANIFEST_COLUMNS = [
    "analysis_id",
    "status",
    "sha256",
    "data_file",
    "yaml_file",
    "genome_assembly",
]

FIXTURE_CODE_SHA = "f" * 40
FIXTURE_METADATA_SHA = "e" * 64


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def make_fixture(tmp: Path) -> tuple[Path, Path, Path]:
    meta = tmp / "meta"
    (meta / "gwas-ssf").mkdir(parents=True, exist_ok=True)
    (meta / "manifests").mkdir(parents=True, exist_ok=True)

    # Sidecars carry the named genome assembly the resolution must match.
    sidecar_by_id: dict[str, Path] = {}
    for ids in MEMBERS.values():
        for analysis_id in ids:
            sidecar = meta / "gwas-ssf" / f"{analysis_id}.tsv.gz-meta.yaml"
            sidecar.write_text(
                yaml.safe_dump({"genome_assembly": "GRCh38"}), encoding="utf-8"
            )
            sidecar_by_id[analysis_id] = sidecar

    mapping_path = tmp / "mapping.tsv"
    write_tsv(
        mapping_path,
        ["analysis_id", "ancestry"],
        [
            {"analysis_id": analysis_id, "ancestry": ancestry}
            for ancestry, ids in MEMBERS.items()
            for analysis_id in ids
        ],
    )

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
                    "source_file": f"/fixture/{analysis_id}.tsv.gz",
                    "source_genome_build": "GRCh38",
                    "source_label": f"{ancestry} trait",
                    "source_analysis_id": analysis_id,
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
            "source_file",
            "source_genome_build",
            "source_label",
            "source_analysis_id",
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
            "planned_manifest": PLANNED_MANIFEST[ancestry],
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
        "metadata": {
            "root": str(meta),
            "code_sha": FIXTURE_CODE_SHA,
            "output_metadata_content_sha256": FIXTURE_METADATA_SHA,
            "membership": 15,
            "association_opens": 0,
            "acceptance": "technical metadata evidence only",
            "candidates_table": str(meta / "candidates.tsv"),
            "mapping": str(mapping_path),
        },
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

    # Fixture planned manifests with real member IDs: the combined European
    # manifest carries EUR+META, and each row binds a data file, a named sidecar
    # and a status/digest.
    manifest_groups = {
        "european.tsv": MEMBERS["EUR"] + MEMBERS["META"],
        "african.tsv": MEMBERS["AFR"],
        "amr.tsv": MEMBERS["AMR"],
        "eas.tsv": MEMBERS["EAS"],
    }
    manifest_counts: dict[str, int] = {}
    for name, ids in manifest_groups.items():
        rows = [
            {
                "analysis_id": analysis_id,
                "status": "ok",
                "sha256": "a" * 64,
                "data_file": f"/fixture/{analysis_id}.tsv.gz",
                "yaml_file": str(sidecar_by_id[analysis_id]),
                "genome_assembly": "GRCh38",
            }
            for analysis_id in ids
        ]
        write_tsv(meta / "manifests" / name, MANIFEST_COLUMNS, rows)
        manifest_counts[name] = len(ids)

    write_tsv(
        meta / "candidates.tsv",
        ["analysis_id"],
        [{"analysis_id": analysis_id} for ids in MEMBERS.values() for analysis_id in ids],
    )
    (meta / "parent-run-summary.json").write_text(
        json.dumps(
            {
                "code_sha": FIXTURE_CODE_SHA,
                "output_metadata_content_sha256": FIXTURE_METADATA_SHA,
                "membership": 15,
                "association_opens": 0,
                "promotion": False,
                "manifest_counts": manifest_counts,
            }
        ),
        encoding="utf-8",
    )
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

    def test_planned_manifest_membership_crosscheck(self) -> None:
        counts = gen.read_planned_manifests(self.config)
        self.assertEqual(counts["european.tsv"], 9)
        self.assertEqual(counts["african.tsv"], 3)
        _, grouped = gen.build_partition(self.config, self.mapping)
        gen.validate_planned_manifests(self.config, grouped, counts)
        with self.assertRaisesRegex(gen.CandidateError, "expected 9"):
            gen.validate_planned_manifests(
                self.config, grouped, {**counts, "european.tsv": 8}
            )
        plan = gen.run_plan(self.config, self.mapping, self.out, counts)
        self.assertEqual(plan["stores"]["EUR"]["planned_manifest_members"], 9)
        self.assertEqual(plan["stores"]["EUR"]["resolution"], "pending")
        self.assertEqual(plan["metadata"]["root"], str(self.tmp / "meta"))

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

    def test_source_snapshot_records_metadata_provenance(self) -> None:
        gen.run_emit(
            self.config, self.mapping, self.resolution, self.out, created_at="2026-10-13T00:00:00+00:00"
        )
        release = yaml.safe_load((self.out / "OGS-00012" / "release.yaml").read_text())
        snapshot = release["source_snapshot"]
        self.assertTrue(snapshot["source_metadata_only"])
        self.assertEqual(snapshot["resolution_table"], str(self.resolution))
        self.assertEqual(len(snapshot["resolution_sha256"]), 64)
        self.assertEqual(snapshot["planned_manifest"], "european.tsv")
        self.assertNotIn("association", str(snapshot).lower().replace("association_opens", ""))

    def test_blank_source_binding_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "eur1":
                row["source_file"] = ""
        bad = self.tmp / "bad.tsv"
        write_tsv(bad, list(rows[0].keys()), rows)
        with self.assertRaisesRegex(gen.CandidateError, "blank required column 'source_file'"):
            gen.run_emit(
                self.config, self.mapping, bad, self.out, created_at="2026-10-13T00:00:00+00:00"
            )

    def test_cross_ancestry_resolution_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "afr1":
                row["assigned_ancestry"] = "EUR"
        bad = self.tmp / "cross.tsv"
        write_tsv(bad, list(rows[0].keys()), rows)
        with self.assertRaisesRegex(gen.CandidateError, "does not match the AFR bucket"):
            gen.run_emit(
                self.config, self.mapping, bad, self.out, created_at="2026-10-13T00:00:00+00:00"
            )

    def test_refuses_registry_store_root(self) -> None:
        with self.assertRaisesRegex(gen.CandidateError, "registry Store root"):
            gen.assert_not_registry_store_root(REPO_ROOT / "stores")

    def test_refuses_symlinked_output_parent(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(gen.CandidateError, "symlinked output"):
            gen.assert_not_registry_store_root(link)

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

    def test_wrong_source_analysis_id_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "eur1":
                row["source_analysis_id"] = "other"
        with self.assertRaisesRegex(gen.CandidateError, "source_analysis_id"):
            self._emit_mutated(rows)

    def test_wrong_source_file_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "eur1":
                row["source_file"] = "/wrong/path.tsv.gz"
        with self.assertRaisesRegex(gen.CandidateError, "!= manifest data_file"):
            self._emit_mutated(rows)

    def test_wrong_source_genome_build_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "eur1":
                row["source_genome_build"] = "GRCh37"
        with self.assertRaisesRegex(gen.CandidateError, "genome_assembly"):
            self._emit_mutated(rows)

    def test_nonfinite_sample_size_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "eur1":
                row["sample_size"] = "NaN"
        with self.assertRaisesRegex(gen.CandidateError, "positive integer"):
            self._emit_mutated(rows)

    def test_same_count_swapped_manifest_id_fails(self) -> None:
        manifest = self.tmp / "meta" / "manifests" / "european.tsv"
        rows = gen.read_tsv(manifest)
        for row in rows:
            if row["analysis_id"] == "eur1":
                row["analysis_id"] = "not-a-member"
        write_tsv(manifest, list(rows[0].keys()), rows)
        with self.assertRaisesRegex(gen.CandidateError, "ID set does not match"):
            gen.run_plan(
                self.config,
                self.mapping,
                self.out,
                gen.read_planned_manifests(self.config),
            )

    def test_forged_config_digest_fails(self) -> None:
        config = gen.load_config(self.config_path)
        config["metadata"]["code_sha"] = "0" * 40
        with self.assertRaisesRegex(gen.CandidateError, "!= receipt"):
            gen.run_plan(
                config,
                self.mapping,
                self.out,
                gen.read_planned_manifests(config),
            )

    def test_meta_row_with_single_ancestry_label_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "meta1":
                row["assigned_ancestry"] = "EUR"
        with self.assertRaisesRegex(gen.CandidateError, "unambiguous empty/META"):
            self._emit_mutated(rows)

    def test_meta_multi_ancestry_alias_is_normalised_empty(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "meta1":
                row["assigned_ancestry"] = "Multi-ancestry"
        self._emit_mutated(rows)
        with (self.out / "OGS-00017" / "analyses.tsv").open(newline="", encoding="utf-8") as handle:
            table = {row["analysis_id"]: row for row in csv.DictReader(handle, delimiter="\t")}
        self.assertEqual(table["meta1"]["assigned_ancestry"], "")

    def test_non_integer_sample_size_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "eur1":
                row["sample_size"] = "1000.5"
        with self.assertRaisesRegex(gen.CandidateError, "positive integer"):
            self._emit_mutated(rows)

    def test_extra_registry_root_is_refused(self) -> None:
        custom = self.tmp / "custom-registry"
        (custom / "stores").mkdir(parents=True)
        with self.assertRaisesRegex(gen.CandidateError, "registry Store root"):
            gen.assert_not_registry_store_root(
                custom / "stores" / "OGS-00099", [custom / "stores"]
            )

    def test_stores_path_component_is_refused(self) -> None:
        with self.assertRaisesRegex(gen.CandidateError, "path component named 'stores'"):
            gen.assert_not_registry_store_root(self.tmp / "stores" / "nested")

    def test_definitions_include_mapping_hash(self) -> None:
        path = gen.write_candidate_definitions(
            self.config, self.out, mapping_path=self.mapping
        )
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(len(document["source_hashes"]["mapping_sha256"]), 64)

    def _emit_mutated(self, rows: list[dict[str, str]]):
        path = self.tmp / "mutated-resolution.tsv"
        write_tsv(path, list(rows[0].keys()), rows)
        return gen.run_emit(
            self.config,
            self.mapping,
            path,
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


class TestSafeWrite(unittest.TestCase):
    """The shared safe-write helper refuses aliases and out-of-root targets."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.root = self.tmp / "out"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_refuses_stale_temp(self) -> None:
        target = self.root / "x.yaml"
        (self.root / "x.yaml.partial").write_text("stale")
        with self.assertRaisesRegex(gen.safe_write.SafeWriteError, "existing temporary"):
            gen.safe_write.atomic_write_text(target, "data", self.root, overwrite=True)

    def test_refuses_symlinked_final(self) -> None:
        other = self.tmp / "other"
        other.write_text("x")
        target = self.root / "x.yaml"
        target.symlink_to(other)
        with self.assertRaisesRegex(gen.safe_write.SafeWriteError, "symlinked final"):
            gen.safe_write.atomic_write_text(target, "data", self.root, overwrite=True)

    def test_refuses_existing_final_without_overwrite(self) -> None:
        target = self.root / "x.yaml"
        target.write_text("old")
        with self.assertRaisesRegex(gen.safe_write.SafeWriteError, "refusing to overwrite"):
            gen.safe_write.atomic_write_text(target, "new", self.root, overwrite=False)

    def test_refuses_target_outside_root(self) -> None:
        with self.assertRaisesRegex(gen.safe_write.SafeWriteError, "escapes"):
            gen.safe_write.ensure_write_within(self.tmp / "escape.yaml", self.root)


if __name__ == "__main__":
    unittest.main()
