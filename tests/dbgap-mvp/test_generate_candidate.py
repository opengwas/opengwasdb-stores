#!/usr/bin/env python3
"""Tests for the MVP ancestry/META candidate emitter (#221).

Covers the ticket-specific contracts with small fixtures:

1. The metadata partition is exact, disjoint and reconciles the legacy European
   bucket as EUR + META.
2. META is emitted observed-only: no ``derived_from`` and no completion block.
3. One bundle per Store id (#308, ADR 0037). A qualified, active reference puts
   both the ``build`` and the ``complete`` block in that one bundle; a pending,
   on-hold or blocked reference leaves the observed bundle exactly as it was and
   records a completion definition keyed to the same id.
4. Every emitted bundle is ``status: accepted`` (dev-buildable, #247) and carries
   an ``accepted_at``. Human acceptance stays explicitly pending in every
   ``validation.yaml`` and there is no build/validation evidence.
5. Missing or extra resolution rows fail closed.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation.providers.dbgap.mvp import generate_candidate as gen
from manifest_generation.providers.dbgap.mvp import partition
from ogstores import bundle as bundle_module
from ogstores import plan as plan_module

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The tree before #308 landed. Seam 2 pins today's observed bundle against the
#: bytes this revision emits from the same fixture, so the pinned values come
#: from the base code and never from the code under test.
BASE_REVISION = "d4f620f"

CREATED_AT = "2026-10-13T00:00:00+00:00"

#: The ids reserved for ADR 0007 two-id completion. Retired unused by #308; no
#: emit may mention them again.
RETIRED_COMPLETED_IDS = ("OGS-00018", "OGS-00019", "OGS-00020", "OGS-00021")

# Fixture membership: small, but the shape mirrors the real buckets.
MEMBERS = {
    "EUR": ["GCST_eur1", "GCST_eur2", "GCST_eur3", "GCST_eur4", "GCST_eur5"],
    "AFR": ["GCST_afr1", "GCST_afr2", "GCST_afr3"],
    "AMR": ["GCST_amr1", "GCST_amr2"],
    "EAS": ["GCST_eas1"],
    "META": ["GCST_meta1", "GCST_meta2", "GCST_meta3", "GCST_meta4"],
}
EXPECTED = {bucket: len(ids) for bucket, ids in MEMBERS.items()}

#: ``observed id``, ``assigned ancestry``, ``source ancestry label``,
#: ``reference qualification``, ``suggested LD panel``.
FIXTURE_STORES = {
    "EUR": ("OGS-00012", "EUR", "European", "pending", "/fixture/ld"),
    "AFR": ("OGS-00013", "AFR", "African", "pending", "/fixture/ld"),
    "AMR": ("OGS-00014", "AMR", "Hispanic or Latin American", "hold", None),
    "EAS": ("OGS-00015", "EAS", "East Asian", "pending", "/fixture/ld"),
    "META": ("OGS-00016", None, "META", "not_applicable", None),
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
    (meta / "manifests").mkdir(parents=True, exist_ok=True)
    (meta / "gwas-ssf").mkdir(parents=True, exist_ok=True)

    # Mirror the parent layout: gwas-ssf/<bucket>/GCST*/{data symlink, meta yaml}.
    sidecar_by_id: dict[str, Path] = {}
    data_by_id: dict[str, Path] = {}
    index = 0
    for ancestry, ids in MEMBERS.items():
        bucket = f"bucket-{ancestry}"
        for analysis_id in ids:
            index += 1
            directory = meta / "gwas-ssf" / bucket / analysis_id
            directory.mkdir(parents=True, exist_ok=True)
            data = directory / f"{analysis_id}.tsv.gz"
            data.symlink_to(f"/fixture/data/GCST{index}.tsv.gz")
            sidecar = directory / f"{analysis_id}.tsv.gz-meta.yaml"
            sidecar.write_text(
                yaml.safe_dump(
                    {
                        "gwas_id": analysis_id,
                        "data_file_name": data.name,
                        "genome_assembly": "GRCh38",
                    }
                ),
                encoding="utf-8",
            )
            sidecar_by_id[analysis_id] = sidecar
            data_by_id[analysis_id] = data

    (meta / "input-metadata-before.json").write_text(
        json.dumps(
            {
                str(path): {"sha256": gen.sha256_file(path), "size": path.stat().st_size}
                for path in sidecar_by_id.values()
            }
        ),
        encoding="utf-8",
    )

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
                    "ancestry_assignment_method": "unassigned" if ancestry == "META" else "af_assigned",
                    "source_ancestry_label": ancestry,
                    "stored_effect_scale": "sd",
                    "sample_size_kind": "total",
                    "sample_size_scope": "analysis_level",
                    "sample_size": "1000",
                    "original_effect_scale": "sd",
                    "original_sd_method": "source_provided",
                    "source_file": str(data_by_id[analysis_id]),
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
            "source_ancestry_label",
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
    for ancestry, (observed, assigned, label_ancestry, qualification, suggested) in FIXTURE_STORES.items():
        stores[ancestry] = {
            "observed_store_id": observed,
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
    manifest_columns = MANIFEST_COLUMNS + ["sample_size"]
    for name, ids in manifest_groups.items():
        rows = [
            {
                "analysis_id": analysis_id,
                "status": "ok",
                "sha256": "a" * 64,
                "data_file": str(data_by_id[analysis_id]),
                "yaml_file": str(sidecar_by_id[analysis_id]),
                "genome_assembly": "GRCh38",
                "sample_size": "1000",
            }
            for analysis_id in ids
        ]
        write_tsv(meta / "manifests" / name, manifest_columns, rows)
        manifest_counts[name] = len(ids)

    write_tsv(
        meta / "candidates.tsv",
        ["analysis_id"],
        [{"analysis_id": analysis_id} for ids in MEMBERS.values() for analysis_id in ids],
    )
    write_tsv(
        meta / "refresh-receipt.tsv",
        ["analysis_id", "note"],
        [
            {"analysis_id": analysis_id, "note": "fixture"}
            for ids in MEMBERS.values()
            for analysis_id in ids
        ],
    )

    content_sha, _ = gen.metadata_content.metadata_content_sha256(meta)
    config = {
        "id_allocation_status": "provisional-draft",
        "owner_ordering_approved": False,
        "intended_bound_total": 15,
        "metadata": {
            "root": str(meta),
            "code_sha": FIXTURE_CODE_SHA,
            "output_metadata_content_sha256": content_sha,
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
        "finngen": {"current_store_id": "OGS-00017", "previous_store_id": "OGS-00016", "renumbered_to": "OGS-00017"},
    }
    config_path = tmp / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (meta / "parent-run-summary.json").write_text(
        json.dumps(
            {
                "code_sha": FIXTURE_CODE_SHA,
                "output_metadata_content_sha256": content_sha,
                "membership": 15,
                "association_opens": 0,
                "promotion": False,
                "store_operations": False,
                "metadata_idempotent": True,
                "source_metadata_unchanged": True,
                "refresh_exit_codes": [0, 0],
                "manifest_counts": manifest_counts,
            }
        ),
        encoding="utf-8",
    )
    return mapping_path, resolution_path, config_path


def emit_with_the_base_generator(
    tmp: Path, config_path: Path, mapping: Path, resolution: Path, out_dir: Path
) -> Path:
    """Emit this fixture with the generator as it stood before #308.

    Only ``--write-goldens`` calls this; the test suite never reads the base tree.
    The bytes come from that code, never from the code under test: `git archive`
    unpacks ``BASE_REVISION``, and running the generator with ``-m`` from that
    tree's own ``src/`` imports its ``manifest_generation`` and its ``ogstores``
    rather than this checkout's installed packages.
    """
    archive = tmp / "base"
    archive.mkdir()
    # Only ``src/`` is needed, and the full archive is ~180 MB of committed
    # Stores and reports. `pixi.toml` and an empty `stores/` satisfy the
    # package's "installed from a checkout" guard; the generator reads neither.
    try:
        tarball = subprocess.run(
            ["git", "archive", BASE_REVISION, "src", "pixi.toml"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f"cannot read the base tree {BASE_REVISION}: {exc.stderr.decode().strip()}. "
            "Regenerating the goldens needs that commit in the clone (a shallow "
            "checkout must fetch full history)."
        ) from exc
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        tar.extractall(archive, filter="data")
    (archive / "stores").mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "manifest_generation.providers.dbgap.mvp.generate_candidate",
            "emit",
            "--config",
            str(config_path),
            "--mapping",
            str(mapping),
            "--resolution",
            str(resolution),
            "--out-dir",
            str(out_dir),
            "--created-at",
            CREATED_AT,
        ],
        cwd=archive / "src",
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"the base generator failed: {result.stderr}")
    return out_dir


#: The five observed Store ids this fixture emits, whatever their reference says.
OBSERVED_IDS: tuple[str, ...] = ("OGS-00012", "OGS-00013", "OGS-00014", "OGS-00015", "OGS-00016")

#: The bundle files seam 2 pins byte for byte.
PINNED_FILES: tuple[str, ...] = ("release.yaml", "build.yaml", "analyses.tsv")

#: What the base code emits for this fixture, committed so the suite needs no
#: git history and no network. See `fixtures/observed-bundles/README.md`.
GOLDEN_ROOT: Path = Path(__file__).resolve().parent / "fixtures" / "observed-bundles"

#: Digests the fixture's random temp directory feeds into, because each hashes a
#: tree keyed by absolute paths (or is a receipt over one). Everything else in
#: `release.yaml` is a byte the code chose, and `build.yaml` and `analyses.tsv`
#: hold no such digest at all.
FIXTURE_DEPENDENT_DIGESTS: tuple[str, ...] = (
    "output_metadata_content_sha256",
    "metadata_receipt_sha256",
    "metadata_fingerprint_sha256",
    "source_metadata_snapshot_sha256",
    "resolution_sha256",
    "planned_manifest_sha256",
)


def normalise_golden(text: str, fixture: Path) -> str:
    """Replace what the fixture's random temp directory decides with placeholders."""
    text = text.replace(str(fixture), "{fixture}")
    for key in FIXTURE_DEPENDENT_DIGESTS:
        text = re.sub(
            rf"^(\s*{key}: )[0-9a-f]{{64}}$", rf"\1{{fixture-digest}}", text, flags=re.MULTILINE
        )
    return text


def write_goldens() -> list[Path]:
    """Emit the observed bundles from `BASE_REVISION` and write the seam-2 goldens.

    Run by hand when the fixture changes; the suite never reads the base tree:

        pixi run -e dev python3 tests/dbgap-mvp/test_generate_candidate.py --write-goldens
    """
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td)
        mapping, resolution, config_path = make_fixture(fixture)
        out = emit_with_the_base_generator(
            fixture, config_path, mapping, resolution, fixture / "base-out"
        )
        written = []
        for store_id in OBSERVED_IDS:
            for name in PINNED_FILES:
                destination = GOLDEN_ROOT / store_id / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    normalise_golden((out / store_id / name).read_text(encoding="utf-8"), fixture),
                    encoding="utf-8",
                )
                written.append(destination)
        return written


def phase_names(steps) -> dict[str, list[str]]:
    """Every step's name, grouped by the phase it says it belongs to."""
    phases: dict[str, list[str]] = {}
    for step in steps:
        phases.setdefault(step.phase, []).append(step.name)
    return phases


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


class FixtureEmissionTestCase(unittest.TestCase):
    """A temp-dir fixture config, and what an emit of it writes (#221)."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.mapping, self.resolution, self.config_path = make_fixture(self.tmp)
        self.config = gen.load_config(self.config_path)
        self.meta = self.tmp / "meta"
        self.out = self.tmp / "out"

    def tearDown(self) -> None:
        self.td.cleanup()

    def emit(self, **kwargs) -> dict:
        return gen.run_emit(
            self.config,
            self.mapping,
            self.resolution,
            self.out,
            created_at=CREATED_AT,
            **kwargs,
        )

    def qualify(self, ancestry: str, *, panel: str | None = "/fixture/ld", **changes) -> None:
        """Make one fixture reference qualified and active, as an owner decision would."""
        reference = self.config["stores"][ancestry]["completion_reference"]
        reference.update(
            {"qualification": "qualified", "activation": "active", "suggested_ld_panel": panel, **changes}
        )

    def document(self, store_id: str, name: str) -> dict:
        return yaml.safe_load((self.out / store_id / name).read_text(encoding="utf-8"))

    def bundle_directories(self) -> list[str]:
        return sorted(path.name for path in self.out.iterdir() if path.is_dir())

    def completion_plan_files(self) -> list[str]:
        return sorted(path.name for path in self.out.glob("*.completion-plan.yaml"))

    def emitted_text(self) -> str:
        """Every emitted file's text, for a claim that a name appears nowhere."""
        return "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(self.out.rglob("*")) if path.is_file()
        )


class TestCandidateEmission(FixtureEmissionTestCase):
    def _sidecar(self, analysis_id: str) -> Path:
        return next((self.meta / "gwas-ssf").glob(f"*/{analysis_id}/{analysis_id}.tsv.gz-meta.yaml"))

    def _data(self, analysis_id: str) -> Path:
        return next((self.meta / "gwas-ssf").glob(f"*/{analysis_id}/{analysis_id}.tsv.gz"))

    def test_plan_mode_validates_and_writes_partition_only(self) -> None:
        plan = gen.run_plan(self.config, self.mapping, self.out)
        self.assertEqual(plan["total"], 15)
        self.assertEqual(plan["legacy_european_bucket"], 9)
        self.assertTrue((self.out / "partition.tsv").is_file())
        self.assertFalse((self.out / "OGS-00012").exists())

    def test_emit_observed_accepted_but_science_pending(self) -> None:
        result = self.emit()
        self.assertEqual(set(result["emitted"]), set(MEMBERS))
        self.assertEqual(sorted(result["checked"]), sorted(result["emitted"].values()))

        # META is observed-only: no parent and no completion recipe. It is
        # `accepted` only so the dev release workflow may build it (#247).
        meta_release = self.document("OGS-00016", "release.yaml")
        self.assertEqual(meta_release["status"], "accepted")
        self.assertEqual(meta_release["accepted_at"], CREATED_AT)
        self.assertIn("not scientific release acceptance", meta_release["notes"])
        self.assertIsNone(meta_release["derived_from"])
        # The observed bundle is buildable; the human acceptance stays pending.
        meta_bundle = gen.bundle_module.load("OGS-00016", registry_root=self.out)
        self.assertTrue(gen.bundle_module.is_buildable(meta_bundle))
        meta_validation = self.document("OGS-00016", "validation.yaml")
        self.assertEqual(
            meta_validation["acceptance"]["human_acceptance"], "pending"
        )
        meta_build = self.document("OGS-00016", "build.yaml")
        self.assertEqual(meta_build["completion_state"], "observed_only")
        self.assertNotIn("complete", meta_build)

        # EUR's reference is only pending: its bundle stays observed-only and
        # buildable, and the completion recipe is a receipt under the same id
        # (ADR 0037). No second id is minted (#308).
        eur_release = self.document("OGS-00012", "release.yaml")
        self.assertIsNone(eur_release["derived_from"])
        self.assertEqual(eur_release["status"], "accepted")
        eur_build = self.document("OGS-00012", "build.yaml")
        self.assertEqual(eur_build["completion_state"], "observed_only")
        self.assertNotIn("complete", eur_build)
        self.assertTrue(
            gen.bundle_module.is_buildable(
                gen.bundle_module.load("OGS-00012", registry_root=self.out)
            )
        )
        eur_definition = yaml.safe_load(
            (self.out / "OGS-00012.completion-plan.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(eur_definition["store_id"], "OGS-00012")
        self.assertFalse((self.out / "OGS-00018").exists())

        # AMR has no declared reference: a pending definition, not a completion.
        amr_plan = yaml.safe_load((self.out / "OGS-00014.completion-plan.yaml").read_text())
        self.assertEqual(amr_plan["status"], "pending")
        self.assertTrue(amr_plan["blocked"])
        self.assertEqual(amr_plan["human_acceptance"], "pending")
        self.assertEqual(amr_plan["reference"]["qualification"], "hold")
        self.assertIsNone(amr_plan["reference"]["resource_id"])

    def test_available_reference_does_not_promote_qualification(self) -> None:
        """A declared `available` reference is not a qualification or science pass."""
        self.emit()
        # A pending reference keeps the observed bundle free of any completion
        # claim; the declared reference is only recorded beside it.
        observed = self.document("OGS-00012", "validation.yaml")
        self.assertNotIn("completion_reference", observed["acceptance"])
        definition = yaml.safe_load(
            (self.out / "OGS-00012.completion-plan.yaml").read_text(encoding="utf-8")
        )
        reference = definition["reference"]
        self.assertEqual(reference["declared_status"], "available")
        self.assertEqual(reference["qualification"], "pending")
        self.assertEqual(reference["activation"], "pending")
        self.assertTrue(definition["blocked"])
        # Nothing in the emitted tree may claim the reference is qualified.
        self.assertNotIn("qualified", self.emitted_text().replace("not qualified", ""))

    def test_missing_reference_still_emits_pending_definition(self) -> None:
        self.emit()
        definition = yaml.safe_load((self.out / "OGS-00014.completion-plan.yaml").read_text())
        self.assertEqual(definition["human_acceptance"], "pending")
        self.assertTrue(definition["blocked"])
        self.assertEqual(definition["store_id"], "OGS-00014")

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
        for ancestry, bucket in definitions["stores"].items():
            self.assertFalse(bucket["observed"]["in_stores"])
            self.assertEqual(bucket["observed"]["acceptance"], "pending")
            if ancestry == "META":
                # Observed-only by policy: no completion definition at all.
                self.assertIsNone(bucket["completed"])
                continue
            completion = bucket["completed"]
            # The definition belongs to the observed Store id (ADR 0037): no
            # second id and no parent (#308).
            self.assertEqual(completion["store_id"], bucket["observed"]["store_id"])
            self.assertNotIn("derived_from", completion)
            self.assertEqual(completion["role"], "completion-definition")
            self.assertFalse(completion["declared"])
            self.assertEqual(completion["human_acceptance"], "pending")
            self.assertTrue(completion["blocked"])
        retired = json.dumps(definitions)
        for completed_id in RETIRED_COMPLETED_IDS:
            self.assertNotIn(completed_id, retired)

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
            if row["analysis_id"] == "GCST_eur1":
                row["source_file"] = ""
        bad = self.tmp / "bad.tsv"
        write_tsv(bad, list(rows[0].keys()), rows)
        with self.assertRaisesRegex(gen.CandidateError, "blank required column 'source_file'"):
            gen.run_emit(
                self.config, self.mapping, bad, self.out, created_at="2026-10-13T00:00:00+00:00"
            )

    def test_missing_source_ancestry_label_column_fails_before_mkdir(self) -> None:
        rows = gen.read_tsv(self.resolution)
        columns = [c for c in rows[0].keys() if c != "source_ancestry_label"]
        stripped = [
            {k: v for k, v in row.items() if k != "source_ancestry_label"} for row in rows
        ]
        path = self.tmp / "no-label.tsv"
        write_tsv(path, columns, stripped)
        out = self.tmp / "out-missing-label"
        with self.assertRaisesRegex(gen.CandidateError, "missing required column"):
            gen.run_emit(
                self.config, self.mapping, path, out, created_at="2026-10-13T00:00:00+00:00"
            )
        self.assertFalse(out.exists())

    def test_blank_source_ancestry_label_fails_before_mkdir(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "GCST_eur1":
                row["source_ancestry_label"] = ""
        path = self.tmp / "blank-label.tsv"
        write_tsv(path, list(rows[0].keys()), rows)
        out = self.tmp / "out-blank-label"
        with self.assertRaisesRegex(
            gen.CandidateError, "blank required column 'source_ancestry_label'"
        ):
            gen.run_emit(
                self.config, self.mapping, path, out, created_at="2026-10-13T00:00:00+00:00"
            )
        self.assertFalse(out.exists())

    def test_mismatched_source_ancestry_label_fails_before_mkdir(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "GCST_afr1":
                row["source_ancestry_label"] = "EUR"
        path = self.tmp / "mismatch-label.tsv"
        write_tsv(path, list(rows[0].keys()), rows)
        out = self.tmp / "out-mismatch-label"
        with self.assertRaisesRegex(gen.CandidateError, "authority mapping bucket"):
            gen.run_emit(
                self.config, self.mapping, path, out, created_at="2026-10-13T00:00:00+00:00"
            )
        self.assertFalse(out.exists())

    def _emit_ancestry_pair(
        self, analysis_id: str, assigned: str, method: str, name: str
    ) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == analysis_id:
                row["assigned_ancestry"] = assigned
                row["ancestry_assignment_method"] = method
        path = self.tmp / f"{name}.tsv"
        write_tsv(path, list(rows[0].keys()), rows)
        out = self.tmp / f"out-{name}"
        with self.assertRaisesRegex(gen.CandidateError, "ancestry_assignment_method"):
            gen.run_emit(
                self.config, self.mapping, path, out, created_at="2026-10-13T00:00:00+00:00"
            )
        self.assertFalse(out.exists())

    def test_meta_canonical_empty_with_af_assigned_fails_before_mkdir(self) -> None:
        self._emit_ancestry_pair("GCST_meta1", "", "af_assigned", "meta-empty-af")

    def test_amr_empty_with_af_assigned_fails_before_mkdir(self) -> None:
        self._emit_ancestry_pair("GCST_amr1", "", "af_assigned", "amr-empty-af")

    def test_assigned_amr_with_unassigned_fails_before_mkdir(self) -> None:
        self._emit_ancestry_pair("GCST_amr1", "AMR", "unassigned", "amr-assigned-unassigned")

    def test_valid_canonical_ancestry_pairs_unchanged(self) -> None:
        gen.run_emit(
            self.config,
            self.mapping,
            self.resolution,
            self.out,
            created_at="2026-10-13T00:00:00+00:00",
        )

    def test_cross_ancestry_resolution_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "GCST_afr1":
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
        rows = [row for row in gen.read_tsv(self.resolution) if row["analysis_id"] != "GCST_eur3"]
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
            if row["analysis_id"] == "GCST_eur1":
                row["source_analysis_id"] = "other"
        with self.assertRaisesRegex(gen.CandidateError, "source_analysis_id"):
            self._emit_mutated(rows)

    def test_wrong_source_file_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "GCST_eur1":
                row["source_file"] = "/wrong/path.tsv.gz"
        with self.assertRaisesRegex(gen.CandidateError, "!= manifest data_file"):
            self._emit_mutated(rows)

    def test_wrong_source_genome_build_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "GCST_eur1":
                row["source_genome_build"] = "GRCh37"
        with self.assertRaisesRegex(gen.CandidateError, "genome_assembly"):
            self._emit_mutated(rows)

    def test_nonfinite_sample_size_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "GCST_eur1":
                row["sample_size"] = "NaN"
        with self.assertRaisesRegex(gen.CandidateError, "positive integer"):
            self._emit_mutated(rows)

    def test_same_count_swapped_manifest_id_fails(self) -> None:
        manifest = self.tmp / "meta" / "manifests" / "european.tsv"
        rows = gen.read_tsv(manifest)
        for row in rows:
            if row["analysis_id"] == "GCST_eur1":
                row["analysis_id"] = "not-a-member"
        write_tsv(manifest, list(rows[0].keys()), rows)
        self._refresh_proof(manifest)
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
            if row["analysis_id"] == "GCST_meta1":
                row["assigned_ancestry"] = "EUR"
        with self.assertRaisesRegex(gen.CandidateError, "unambiguous empty/META"):
            self._emit_mutated(rows)

    def test_meta_multi_ancestry_alias_is_normalised_empty(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "GCST_meta1":
                row["assigned_ancestry"] = "Multi-ancestry"
        self._emit_mutated(rows)
        with (self.out / "OGS-00016" / "analyses.tsv").open(newline="", encoding="utf-8") as handle:
            table = {row["analysis_id"]: row for row in csv.DictReader(handle, delimiter="\t")}
        self.assertEqual(table["GCST_meta1"]["assigned_ancestry"], "")

    def test_non_integer_sample_size_fails(self) -> None:
        rows = gen.read_tsv(self.resolution)
        for row in rows:
            if row["analysis_id"] == "GCST_eur1":
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

    def test_missing_sidecar_genome_assembly_fails(self) -> None:
        sidecar = self._sidecar("GCST_eur1")
        sidecar.write_text(
            yaml.safe_dump({"gwas_id": "GCST_eur1", "data_file_name": "GCST_eur1.tsv.gz"}),
            encoding="utf-8",
        )
        self._refresh_proof(sidecar)
        with self.assertRaisesRegex(gen.CandidateError, "no genome_assembly"):
            self._emit_mutated(gen.read_tsv(self.resolution))

    def test_tampered_sidecar_genome_fails(self) -> None:
        sidecar = self._sidecar("GCST_eur1")
        sidecar.write_text(
            yaml.safe_dump(
                {
                    "gwas_id": "GCST_eur1",
                    "data_file_name": "GCST_eur1.tsv.gz",
                    "genome_assembly": "GRCh37",
                }
            ),
            encoding="utf-8",
        )
        self._refresh_proof(sidecar)
        with self.assertRaisesRegex(gen.CandidateError, "assembly disagreement"):
            self._emit_mutated(gen.read_tsv(self.resolution))

    def test_tampered_source_fingerprint_fails(self) -> None:
        sidecar = self._sidecar("GCST_eur1")
        sidecar.write_text(
            yaml.safe_dump(
                {
                    "gwas_id": "GCST_eur1",
                    "data_file_name": "GCST_eur1.tsv.gz",
                    "genome_assembly": "GRCh38",
                    "note": "tampered after the frozen snapshot",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(gen.CandidateError, "content sha"):
            self._emit_mutated(gen.read_tsv(self.resolution))

    def test_tampered_manifest_same_ids_fails(self) -> None:
        manifest = self.meta / "manifests" / "european.tsv"
        rows = gen.read_tsv(manifest)
        rows[0]["sha256"] = "b" * 64
        write_tsv(manifest, list(rows[0].keys()), rows)
        with self.assertRaisesRegex(gen.CandidateError, "content sha"):
            self._emit_mutated(gen.read_tsv(self.resolution))

    def test_tampered_data_link_target_fails(self) -> None:
        data = self._data("GCST_eur1")
        data.unlink()
        data.symlink_to("/fixture/data/tampered.tsv.gz")
        with self.assertRaisesRegex(gen.CandidateError, "content sha"):
            self._emit_mutated(gen.read_tsv(self.resolution))

    def test_missing_source_snapshot_fails(self) -> None:
        (self.meta / "input-metadata-before.json").unlink()
        with self.assertRaisesRegex(gen.CandidateError, "snapshot"):
            self._emit_mutated(gen.read_tsv(self.resolution))

    def _refresh_proof(self, *paths: Path) -> None:
        snapshot_path = self.meta / "input-metadata-before.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        for path in paths:
            snapshot[str(path)] = {
                "sha256": gen.sha256_file(path),
                "size": path.stat().st_size,
            }
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        content_sha, _ = gen.metadata_content.metadata_content_sha256(self.meta)
        receipt = self.meta / "parent-run-summary.json"
        document = json.loads(receipt.read_text(encoding="utf-8"))
        document["output_metadata_content_sha256"] = content_sha
        receipt.write_text(json.dumps(document), encoding="utf-8")
        self.config["metadata"]["output_metadata_content_sha256"] = content_sha

    def test_missing_receipt_field_fails(self) -> None:
        receipt = self.tmp / "meta" / "parent-run-summary.json"
        document = json.loads(receipt.read_text(encoding="utf-8"))
        del document["metadata_idempotent"]
        receipt.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(gen.CandidateError, "metadata_idempotent"):
            gen.verify_metadata_snapshot(self.config)

    def test_missing_candidates_file_fails(self) -> None:
        (self.tmp / "meta" / "candidates.tsv").unlink()
        with self.assertRaisesRegex(gen.CandidateError, "candidates_table file not found"):
            gen.metadata_hashes(self.config, self.mapping)

    def test_expected_fingerprint_mismatch_fails(self) -> None:
        with self.assertRaisesRegex(gen.CandidateError, "fingerprint does not match"):
            gen.run_emit(
                self.config,
                self.mapping,
                self.resolution,
                self.out,
                created_at="2026-10-13T00:00:00+00:00",
                expected_metadata_fingerprint="0" * 64,
            )

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


class TestOneBundlePerStore(FixtureEmissionTestCase):
    """#308: reference completion replaces its Store, so one id gets one bundle.

    ADR 0037 supersedes ADR 0007's second, ``derived_from``-linked completed id.
    The generator therefore never mints a completed id: a qualified reference
    puts both phases in the observed id's bundle, and a pending one leaves that
    bundle exactly as it was with a recorded completion definition beside it.
    """

    def test_a_qualified_reference_emits_one_bundle_under_the_observed_id(self) -> None:
        self.qualify("EUR")

        # The contract for this shape lands in #304; the bytes are what this
        # ticket owes, so they are asserted before the contract can check them.
        result = self.emit(check=False)

        self.assertEqual(set(result["emitted"]), set(MEMBERS))
        self.assertEqual(result["emitted"]["EUR"], "OGS-00012")
        self.assertEqual(
            self.bundle_directories(),
            ["OGS-00012", "OGS-00013", "OGS-00014", "OGS-00015", "OGS-00016"],
        )
        release = self.document("OGS-00012", "release.yaml")
        self.assertIsNone(release["derived_from"])
        self.assertEqual(release["status"], "accepted")
        self.assertEqual(release["accepted_at"], CREATED_AT)
        build = self.document("OGS-00012", "build.yaml")
        self.assertEqual(build["completion_state"], "reference_completed")
        self.assertEqual(build["post"], {"top_hits": True, "overview": True})
        self.assertEqual(build["build"]["command"], "build-dense-vcf")
        self.assertEqual(build["complete"]["command"], "complete-dense")
        self.assertEqual(
            build["complete"]["options"], {"ancestry": "EUR", "ld-panel": "/fixture/ld"}
        )
        validation = self.document("OGS-00012", "validation.yaml")
        self.assertEqual(
            validation["acceptance"]["completion_reference"]["activation"], "active"
        )
        # The bundle declares completion, so no separate definition is written.
        self.assertEqual(
            self.completion_plan_files(),
            [
                "OGS-00013.completion-plan.yaml",
                "OGS-00014.completion-plan.yaml",
                "OGS-00015.completion-plan.yaml",
            ],
        )
        # The other Stores are untouched: observed-only, no completion block.
        for store_id in ("OGS-00013", "OGS-00014", "OGS-00015", "OGS-00016"):
            build = self.document(store_id, "build.yaml")
            self.assertEqual(build["completion_state"], "observed_only", store_id)
            self.assertNotIn("complete", build, store_id)

    def test_a_qualified_reference_without_a_declared_panel_is_refused(self) -> None:
        self.qualify("EUR", panel=None)

        with self.assertRaisesRegex(gen.CandidateError, "ld-panel"):
            self.emit(check=False)

        # Nothing is left behind: a recipe is validated before any write.
        self.assertFalse(self.out.exists())

    def test_a_pending_reference_emits_todays_observed_bundles_byte_for_byte(self) -> None:
        # The goldens are what `d4f620f` emitted from this same fixture
        # (`--write-goldens`); the fixture's random temp directory and the six
        # digests that hash it are placeholders, so every other byte is pinned.
        # The generator's command line reads the planned manifests itself, so
        # compare like with like.
        self.emit(manifest_counts=gen.read_planned_manifests(self.config))

        for store_id in OBSERVED_IDS:
            for name in PINNED_FILES:
                emitted = normalise_golden(
                    (self.out / store_id / name).read_text(encoding="utf-8"), self.tmp
                )
                golden = (GOLDEN_ROOT / store_id / name).read_text(encoding="utf-8")
                self.assertEqual(
                    emitted, golden, f"{store_id}/{name} differs from the base code's bytes"
                )
        # The completion definition now belongs to the observed id.
        self.assertEqual(
            self.completion_plan_files(),
            [
                "OGS-00012.completion-plan.yaml",
                "OGS-00013.completion-plan.yaml",
                "OGS-00014.completion-plan.yaml",
                "OGS-00015.completion-plan.yaml",
            ],
        )
        definition = yaml.safe_load(
            (self.out / "OGS-00012.completion-plan.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(definition["store_id"], "OGS-00012")
        self.assertNotIn("derived_from", definition)
        self.assertEqual(definition["status"], "pending")
        self.assertTrue(definition["blocked"])
        self.assertEqual(definition["human_acceptance"], "pending")
        self.assertEqual(definition["reference"]["qualification"], "pending")
        self.assertEqual(definition["reference"]["activation"], "pending")
        self.assertIn("Do not execute", definition["note"])
        # No bundle claims a completion while its reference is pending.
        for store_id in self.bundle_directories():
            self.assertNotIn(
                "complete",
                self.document(store_id, "build.yaml"),
                f"{store_id} declares a completion its reference does not qualify",
            )

    def test_only_the_observed_ids_are_emitted_and_nothing_is_overwritten(self) -> None:
        result = self.emit()

        self.assertEqual(
            sorted(result["emitted"].values()),
            ["OGS-00012", "OGS-00013", "OGS-00014", "OGS-00015", "OGS-00016"],
        )
        emitted_text = self.emitted_text()
        for retired in RETIRED_COMPLETED_IDS:
            self.assertNotIn(retired, emitted_text, f"{retired} is retired and never reused")
        before = {
            path: path.read_bytes() for path in sorted(self.out.rglob("*")) if path.is_file()
        }

        with self.assertRaisesRegex(gen.CandidateError, "refusing to overwrite"):
            self.emit()

        self.assertEqual(
            {path: path.read_bytes() for path in sorted(self.out.rglob("*")) if path.is_file()},
            before,
        )

    def test_a_config_that_still_declares_a_completed_id_is_refused(self) -> None:
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["stores"]["EUR"]["completed_store_id"] = "OGS-00018"
        retired = self.tmp / "retired.yaml"
        retired.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        with self.assertRaisesRegex(gen.CandidateError, "ADR 0037"):
            gen.load_config(retired)
        with self.assertRaisesRegex(gen.CandidateError, "ADR 0037"):
            gen.run_emit(config, self.mapping, self.resolution, self.out, created_at=CREATED_AT)
        self.assertFalse(self.out.exists())

    def test_the_tracked_config_declares_no_completed_store_id(self) -> None:
        path = REPO_ROOT / "resources" / "generators" / "dbgap-mvp" / "config-candidate.yaml"
        tracked = gen.load_config(path)

        for ancestry, store in tracked["stores"].items():
            self.assertNotIn("completed_store_id", store, ancestry)
        allocation = RETIRED_COMPLETED_IDS[0]
        self.assertIn(allocation, path.read_text(encoding="utf-8"))

    def test_a_reference_that_is_not_qualified_is_blocked_however_its_activation_reads(
        self,
    ) -> None:
        """Blocking is the whole eligibility rule, not activation alone (#308 review)."""
        reference = self.config["stores"]["EUR"]["completion_reference"]
        reference.update({"qualification": "pending", "activation": "active"})

        self.emit()

        definition = yaml.safe_load(
            (self.out / "OGS-00012.completion-plan.yaml").read_text(encoding="utf-8")
        )
        self.assertTrue(definition["blocked"])
        self.assertNotIn("complete", self.document("OGS-00012", "build.yaml"))
        tracked = gen.render_candidate_definitions(self.config)["stores"]["EUR"]["completed"]
        self.assertTrue(tracked["blocked"])
        self.assertFalse(tracked["declared"])

    def test_a_reference_with_no_resource_is_blocked_however_its_activation_reads(self) -> None:
        reference = self.config["stores"]["EUR"]["completion_reference"]
        reference.update(
            {"resource_id": None, "qualification": "qualified", "activation": "active"}
        )

        self.emit()

        definition = yaml.safe_load(
            (self.out / "OGS-00012.completion-plan.yaml").read_text(encoding="utf-8")
        )
        self.assertTrue(definition["blocked"])
        tracked = gen.render_candidate_definitions(self.config)["stores"]["EUR"]["completed"]
        self.assertTrue(tracked["blocked"])
        self.assertFalse(tracked["declared"])

    def test_the_plan_names_the_completion_state_each_store_will_declare(self) -> None:
        plan = gen.run_plan(self.config, self.mapping, self.out)

        self.assertNotIn("completed_store_id", plan["stores"]["EUR"])
        self.assertEqual(plan["stores"]["EUR"]["completion_state"], "observed_only")
        self.assertEqual(plan["stores"]["META"]["completion_state"], "observed_only")

        self.qualify("EUR")
        planned = gen.run_plan(self.config, self.mapping, self.tmp / "qualified")
        self.assertEqual(planned["stores"]["EUR"]["completion_state"], "reference_completed")

    def test_the_single_id_bundle_passes_the_contract_and_plans_both_phases(self) -> None:
        """#308's last check, against #304's contract and its real plan."""
        self.qualify("EUR")
        self.emit(check=False)

        self.assertEqual(gen.check_bundle(self.out / "OGS-00012", self.out), [])
        loaded = bundle_module.load("OGS-00012", registry_root=self.out)
        # #304 returns one list: the build phase, then the completion phase.
        steps = plan_module.plan(loaded, artifact_root=self.tmp / "artifacts")

        # The fixture declares `post: {top_hits, overview}`, and `validate` is
        # the planner's post default. The build phase is `build` and those post
        # steps; the completion phase is `complete` and the completion-named
        # copies of them. `complete-dense` builds top hits inline (ADR 0037), so
        # there is no `completion-top-hits`.
        self.assertEqual(
            phase_names(steps),
            {
                "build": ["build", "top-hits", "overview", "validate"],
                "completion": ["complete", "completion-overview", "completion-validate"],
            },
        )
        # The build phase runs to completion before the completion phase starts.
        self.assertEqual(
            [step.phase for step in steps],
            ["build"] * 4 + ["completion"] * 3,
        )
        complete = next(step for step in steps if step.name == "complete")
        self.assertEqual(complete.argv[:2], ["opengwasdb", "complete-dense"])


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
    if "--write-goldens" in sys.argv[1:]:
        for path in write_goldens():
            print(path.relative_to(REPO_ROOT))
    else:
        unittest.main()
