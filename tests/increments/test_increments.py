#!/usr/bin/env python3
"""Increment Bundles: Analyses join and leave a published Hybrid under its Store id (ADR 0045, #546).

Tested at the public entry points:

- the Release Bundle contract (`bundle.check`, and `bundle-check` through
  `manifest_generation.check_bundles`) holds every `increments/<nnnn>/`;
- `ogstores.increment.fold` is the pure fold the increment route applies at
  publication;
- `ogstores.amendment` accepts a declared, published increment and still
  refuses an undeclared membership change, holding the baseline to the
  `work/` files the publication recorded (`membership_changes`), and checks an
  accepted increment against the published membership and a splice's report
  (`increment_changes`);
- the emit's increment mode (`manifest_generation.emit.emit_increment` and its
  command line) writes only `increments/<nnnn>/`;
- the attestation writer (`python3 -m ogstores.source_record`) writes
  `source-record.json` from the published release's records and the owner's
  attested digests, and refuses without them.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from manifest_generation import check_bundles
from manifest_generation import emit as emitter
from manifest_generation import generate_candidate
from ogstores import amendment, bundle, increment, manifest, paths, source_record

STORE_ID = "OGS-00091"
COLUMNS = (
    "analysis_index", "analysis_id", "stored_effect_scale", "sample_size_kind", "sample_size_scope",
    "sample_size", "original_effect_scale", "original_sd_method", "assigned_ancestry",
    "ancestry_assignment_method", "checksum", "checksum_algorithm", "source_file", "source_genome_build",
    "inclusion_reason", "exclude_from_build",
)
QC_HEADER = ("analysis_id", "exclude_from_build", "exclusion_reason", "eaf_orientation", "eaf_orientation_measured")
EXCLUSIONS_HEADER = ("analysis_id", "source_analysis_id", "category", "reason", "detail", "exclude_from_build")
ANCESTRY_HEADER = ("analysis_id", "assigned_ancestry")
SHA_VR = "c" * 64
FINGERPRINT = {"algorithm": "opengwasdb-release-fingerprint-v1", "sha256": "d" * 64, "n_files": 10}


def row(analysis_id: str, *, excluded: str = "", seed: str = "a") -> dict[str, str]:
    """One analyses.tsv row; `excluded` is the exclusion code, empty for an admitted row."""
    return {
        "analysis_index": "", "analysis_id": analysis_id, "stored_effect_scale": "sd",
        "sample_size_kind": "total", "sample_size_scope": "analysis_level", "sample_size": "1000",
        "original_effect_scale": "sd", "original_sd_method": "declared_standardised", "assigned_ancestry": "EUR",
        "ancestry_assignment_method": "af_assigned",
        "checksum": hashlib.sha256(f"{seed}{analysis_id}".encode()).hexdigest(), "checksum_algorithm": "sha256",
        "source_file": f"/data/source/{analysis_id}.tsv", "source_genome_build": "GRCh38",
        "inclusion_reason": f"excluded: {excluded}: fixture" if excluded else "",
        "exclude_from_build": "true" if excluded else "",
    }


def tsv(header: tuple[str, ...] | list[str], rows: list[dict[str, str]]) -> str:
    return "\n".join(["\t".join(header), *("\t".join(r.get(c, "") for c in header) for r in rows)]) + "\n"


def code_of(r: dict[str, str]) -> str:
    return r["inclusion_reason"].split(": ")[1] if r["exclude_from_build"] == "true" else ""


def table_texts(rows: list[dict[str, str]]) -> dict[str, str]:
    """analyses.tsv and its three sidecars for `rows`, with `analysis_index` numbered 0..n-1."""
    numbered = [{**r, "analysis_index": str(i)} for i, r in enumerate(rows)]
    qc = [
        {"analysis_id": r["analysis_id"], "exclude_from_build": r["exclude_from_build"],
         "exclusion_reason": code_of(r), "eaf_orientation": "passed", "eaf_orientation_measured": "true"}
        for r in rows
    ]
    exclusions = [
        {"analysis_id": r["analysis_id"], "source_analysis_id": r["analysis_id"], "category": "effect_scale",
         "reason": code_of(r), "detail": "fixture", "exclude_from_build": "true"}
        for r in rows if r["exclude_from_build"] == "true"
    ]
    ancestry = [{"analysis_id": r["analysis_id"], "assigned_ancestry": "EUR"} for r in rows]
    return {
        "analyses.tsv": tsv(COLUMNS, numbered),
        "sidecars/quality_checks.tsv": tsv(QC_HEADER, qc),
        "sidecars/exclusions.tsv": tsv(EXCLUSIONS_HEADER, exclusions),
        "sidecars/ancestry.tsv": tsv(ANCESTRY_HEADER, ancestry),
    }


def write_texts(root: Path, texts: dict[str, str]) -> None:
    for relative, text in texts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def write_hybrid_bundle(registry: Path, *, store_id: str = STORE_ID, status: str = "validated",
                        layout: str = "hybrid", rows: list[dict[str, str]] | None = None) -> Path:
    root = registry / store_id
    root.mkdir(parents=True)
    release = {
        "store_id": store_id, "label": "fixture-hybrid", "association_coverage": "gwas", "status": status,
        "source_snapshot_id": "fixture-snapshot-v1", "derived_from": None, "created_at": "2026-11-01T00:00:00Z",
        "description": "Fixture Hybrid Release Bundle", "generator": {"commands": ["fixtures/generate.py"]},
    }
    build: dict[str, Any] = {
        "store_id": store_id, "layout": layout, "completion_state": "reference_completed",
        "build": {"command": "build-hybrid" if layout == "hybrid" else "build-dense-vcf", "options": {}},
        "complete": {"command": "complete-hybrid" if layout == "hybrid" else "complete-dense",
                     "options": {"ld-panel": "/data/ld", "ancestry": "EUR"}},
        "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
    }
    (root / "release.yaml").write_text(yaml.safe_dump(release), encoding="utf-8")
    (root / "build.yaml").write_text(yaml.safe_dump(build), encoding="utf-8")
    (root / "validation.yaml").write_text(yaml.safe_dump({"status": "passed", "checks": {"store": "passed"}}),
                                          encoding="utf-8")
    base = rows if rows is not None else [row("A1"), row("A2"), row("X1", excluded="no_build_eligible_rows")]
    write_texts(root, table_texts(base))
    return root


def write_increment(bundle_root: Path, number: int = 1, *, rows: list[dict[str, str]] | None = None,
                    drops: list[dict[str, Any]] | None = None, **declaration: Any) -> Path:
    directory = bundle_root / increment.INCREMENTS_DIRNAME / f"{number:04d}"
    directory.mkdir(parents=True)
    added = rows if rows is not None else [row("N1"), row("R1", excluded="ancestry_not_eur")]
    if added:
        write_texts(directory, table_texts(added))
    document: dict[str, Any] = {
        "increment": number, "store_id": bundle_root.name, "status": "candidate",
        "reason": "fixture increment", "created_at": "2026-11-29T00:00:00Z",
        "require_added": [], "drops": drops or [], "erratum": None,
        "acceptance": None, "accepted_warnings": [], "published": None,
    }
    if added:
        document["generator"] = {"version": "sha256:" + "b" * 64, "commands": ["fixtures/generate.py --increment"]}
        document["source_snapshot"] = {"inventory_snapshot_id": "fixture-snapshot-v2"}
    document.update(declaration)
    (directory / increment.INCREMENT_FILE).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return directory


SOURCE_RECORD = {
    "rsid_conflict_policy": "drop", "source_assembly": "hg38", "variant_reference_sha256": SHA_VR,
    "reference_panel_sha256": None, "ld_panel_digest": "ldpanel:" + "e" * 64,
    "indexed_subsets": {"hm3": {"variant_list_path": "/data/hm3.txt", "overflow_only": "skip"}},
    "attested_by": "Owner Name", "evidence": {
        "rsid_conflict_policy": "records/build.json argv --on-rsid-conflict; validation.yaml",
        "source_assembly": "work/analyses.tsv effective assembly",
        "variant_reference_sha256": "owner attestation",
        "reference_panel_sha256": "build argv has no --reference-panel",
        "ld_panel_digest": "owner attestation",
        "indexed_subsets": {"hm3": "records/completion-indexed-subset-hm3.json argv"},
    },
}


def edit_increment(directory: Path, **changes: Any) -> None:
    path = directory / increment.INCREMENT_FILE
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def accept(directory: Path, *, reviewed: list[str] | None = None, with_source_record: bool = True) -> None:
    """The owner's acceptance, bound to the increment's ids and bytes."""
    if with_source_record and not (directory / increment.SOURCE_RECORD_FILE).exists():
        (directory / increment.SOURCE_RECORD_FILE).write_text(json.dumps(SOURCE_RECORD, indent=2), encoding="utf-8")
    loaded = yaml.safe_load((directory / increment.INCREMENT_FILE).read_text(encoding="utf-8"))
    ids = reviewed
    if ids is None:
        ids = [d["analysis_id"] for d in loaded.get("drops") or []]
        if (directory / "analyses.tsv").exists():
            ids += [line.split("\t")[1] for line in (directory / "analyses.tsv").read_text().splitlines()[1:]]
    edit_increment(directory, status="accepted", acceptance={
        "accepted_by": "Owner Name", "accepted_at": "2026-11-29T12:00:00Z",
        "reviewed_analysis_ids": sorted(ids), "content_sha256": increment.content_sha256(directory),
    })


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materialise_work(bundle_root: Path, artifacts: Path) -> None:
    store_id = bundle_root.name
    manifest.materialise_build_manifest(
        bundle_root / "analyses.tsv",
        paths.build_manifest_path(store_id, root=artifacts),
        paths.build_manifest_sidecar_path(store_id, root=artifacts),
    )


def publish(bundle_root: Path, number: int, artifacts: Path) -> None:
    """What the route does at publication, for the bundle and `work/` files (ADR 0045 §6)."""
    loaded = bundle.load(bundle_root.name, registry_root=bundle_root.parent)
    write_texts(bundle_root, increment.fold(loaded, number))
    materialise_work(bundle_root, artifacts)
    store_id = bundle_root.name
    directory = bundle_root / increment.INCREMENTS_DIRNAME / f"{number:04d}"
    edit_increment(directory, status="published", published={
        "release_id": f"release-{number}", "source_release_id": f"release-{number - 1}",
        "increment_release_id": f"increment-{number}", "source_fingerprint": FINGERPRINT,
        "published_at": "2026-11-30T00:00:00Z", "archive": f"/data/stores/{store_id}/replaced/2026113{number}T000000Z",
        "manifest_sha256": sha256_file(paths.build_manifest_path(store_id, root=artifacts)),
        "exclusions_sha256": sha256_file(paths.build_manifest_sidecar_path(store_id, root=artifacts)),
    })
    register_p = paths.record_path(store_id, "completion-register", root=artifacts)
    register = json.loads(register_p.read_text()) if register_p.is_file() else {"step": "completion-register"}
    register.setdefault("increments", []).append({
        "increment": number,
        "manifest_sha256": sha256_file(paths.build_manifest_path(store_id, root=artifacts)),
        "exclusions_sha256": sha256_file(paths.build_manifest_sidecar_path(store_id, root=artifacts)),
    })
    register_p.parent.mkdir(parents=True, exist_ok=True)
    register_p.write_text(json.dumps(register))


def forced_rebuild(bundle_root: Path, artifacts: Path) -> None:
    """What `--config force=1` leaves: work/ rematerialised from the bundle, a register record with no increments."""
    materialise_work(bundle_root, artifacts)
    register_p = paths.record_path(bundle_root.name, "completion-register", root=artifacts)
    register_p.write_text(json.dumps({"step": "completion-register", "inputs": []}))


def write_selection(path: Path, ids: list[str], column: str = "accession") -> Path:
    """An explicit increment roster, in the shape of #557's joiners table."""
    path.write_text("\n".join([f"{column}\ttrait", *(f"{a}\tTrait {a}" for a in ids)]) + "\n", encoding="utf-8")
    return path


class IncrementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self._tmp.name) / "stores"
        self.registry.mkdir()
        self.artifacts = Path(self._tmp.name) / "artifacts"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load(self, store_id: str = STORE_ID) -> bundle.Bundle:
        return bundle.load(store_id, registry_root=self.registry)

    def errors(self, store_id: str = STORE_ID) -> list[str]:
        return list(bundle.check(self.load(store_id), registry_root=self.registry))

    def assert_refused(self, fragment: str, store_id: str = STORE_ID) -> None:
        errors = self.errors(store_id)
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")


class TestTheContract(IncrementTestCase):
    def test_a_bundle_with_no_increments_and_a_candidate_increment_both_pass(self) -> None:
        root = write_hybrid_bundle(self.registry)
        self.assertEqual(self.errors(), [])
        before = (root / "analyses.tsv").read_bytes()
        write_increment(root)
        self.assertEqual(self.errors(), [])
        self.assertEqual((root / "analyses.tsv").read_bytes(), before)

    def test_bundle_check_runs_the_increment_contract(self) -> None:
        root = write_hybrid_bundle(self.registry)
        write_increment(root, rows=[row("A1")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_bundles.main(["--registry-root", str(self.registry)])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("already in the bundle", out.getvalue())

    def test_the_increments_columns_must_be_the_bundles(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        lines = [line.split("\t") for line in (directory / "analyses.tsv").read_text().splitlines()]
        swapped = ["\t".join([*fields[:4], fields[5], fields[4], *fields[6:]]) for fields in lines]
        (directory / "analyses.tsv").write_text("\n".join(swapped) + "\n", encoding="utf-8")
        self.assert_refused("columns are not the bundle's columns")

    def test_a_replace_or_readmission_is_refused_until_a_ticket_needs_one(self) -> None:
        for existing in ("A1", "X1"):
            with self.subTest(existing=existing):
                shutil.rmtree(self.registry / STORE_ID, ignore_errors=True)
                root = write_hybrid_bundle(self.registry)
                write_increment(root, rows=[row(existing, seed="b")])
                self.assert_refused(f"{existing!r} is already in the bundle")

    def test_numbering_open_increments_and_stray_entries(self) -> None:
        root = write_hybrid_bundle(self.registry)
        write_increment(root, 2)
        self.assert_refused("numbered from 1 with no gaps")
        shutil.rmtree(root / "increments")
        write_increment(root, 1)
        write_increment(root, 2, rows=[row("N2")])
        self.assert_refused("at most one open increment")
        shutil.rmtree(root / "increments")
        (root / "increments" / "first").mkdir(parents=True)
        self.assert_refused("not a four-digit increment directory")

    def test_only_a_reference_completed_hybrid_takes_an_increment(self) -> None:
        root = write_hybrid_bundle(self.registry, layout="dense")
        write_increment(root)
        self.assert_refused("a Reference-Completed Hybrid")

    def test_an_increment_needs_a_validated_bundle(self) -> None:
        root = write_hybrid_bundle(self.registry, status="accepted")
        write_increment(root)
        self.assert_refused("validated")

    def test_drops_name_admitted_analyses_and_a_defect_needs_an_erratum(self) -> None:
        root = write_hybrid_bundle(self.registry)
        write_increment(root, rows=[], drops=[{"analysis_id": "X1", "reason": "fixture", "defect": False}])
        self.assert_refused("'X1' is not an admitted Analysis")
        shutil.rmtree(root / "increments")
        write_increment(root, rows=[], drops=[{"analysis_id": "A1", "reason": "z mis-scaled", "defect": True}])
        self.assert_refused("erratum")
        edit_increment(root / "increments" / "0001", erratum={"issue": "opengwas/opengwasdb-stores#532", "text": "x"})
        self.assertEqual(self.errors(), [])

    def test_a_required_id_must_be_admitted(self) -> None:
        root = write_hybrid_bundle(self.registry)
        write_increment(root, require_added=["R1"])
        self.assert_refused("require_added 'R1' is not an admitted row")

    def test_the_increments_sidecars_carry_the_bundles_headers_and_ancestry_evidence(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        (directory / "sidecars" / "ancestry.tsv").write_text("analysis_id\tassigned_ancestry\nN1\tEUR\n")
        self.assert_refused("no ancestry evidence for 'R1'")
        (directory / "sidecars" / "ancestry.tsv").write_text("analysis_id\tancestry\nN1\tEUR\nR1\tEUR\n")
        self.assert_refused("header")

    def test_acceptance_is_bound_to_the_reviewed_ids_and_the_bytes(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        edit_increment(directory, status="accepted")
        self.assert_refused("acceptance")
        accept(directory)
        self.assertEqual(self.errors(), [])

        (directory / "analyses.tsv").write_text((directory / "analyses.tsv").read_text().replace("\t1000\t", "\t2000\t"))
        self.assert_refused("content_sha256")

    def test_the_reviewed_ids_are_exactly_the_increments_rows_and_drops(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        accept(directory, reviewed=["N1"])
        self.assert_refused("reviewed_analysis_ids")

    def test_an_accepted_increment_carries_an_attested_source_record(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        accept(directory, with_source_record=False)
        self.assert_refused("source-record.json")
        shutil.rmtree(root / "increments")
        directory = write_increment(root)
        (directory / increment.SOURCE_RECORD_FILE).write_text(json.dumps({**SOURCE_RECORD, "attested_by": ""}))
        accept(directory)
        self.assert_refused("attested_by")

    def test_increment_status_transitions(self) -> None:
        self.assertEqual(increment.validate_transition("candidate", "accepted"), [])
        self.assertEqual(increment.validate_transition("accepted", "published"), [])
        self.assertEqual(increment.validate_transition("accepted", "withdrawn"), [])
        self.assertNotEqual(increment.validate_transition("candidate", "published"), [])
        self.assertNotEqual(increment.validate_transition("published", "candidate"), [])
        self.assertNotEqual(increment.validate_transition("published", "withdrawn"), [])

    def test_a_published_flag_needs_its_published_block(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        accept(directory)
        edit_increment(directory, status="published")
        self.assert_refused("published")


class TestTheFold(IncrementTestCase):
    def test_it_appends_rows_and_sets_drops_leaving_every_other_byte(self) -> None:
        root = write_hybrid_bundle(self.registry, rows=[row("A1"), row("A2"), row("A3"),
                                                         row("X1", excluded="no_build_eligible_rows")])
        directory = write_increment(root, drops=[{"analysis_id": "A2", "reason": "z mis-scaled", "defect": True}],
                                    erratum={"issue": "opengwas/opengwasdb-stores#532", "text": "mis-scaled"})
        accept(directory)
        before = (root / "analyses.tsv").read_text().splitlines()

        texts = increment.fold(self.load(), 1)

        after = texts["analyses.tsv"].splitlines()
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[3:5], before[3:5])
        dropped = dict(zip(COLUMNS, after[2].split("\t")))
        self.assertEqual(dropped["exclude_from_build"], "true")
        self.assertEqual(dropped["inclusion_reason"], "excluded: removed_in_increment_0001: z mis-scaled")
        appended = [dict(zip(COLUMNS, line.split("\t"))) for line in after[5:]]
        self.assertEqual([(r["analysis_index"], r["analysis_id"]) for r in appended], [("4", "N1"), ("5", "R1")])
        qc = [line.split("\t") for line in texts["sidecars/quality_checks.tsv"].splitlines()]
        self.assertEqual([r[0] for r in qc[1:]], ["A1", "A2", "A3", "X1", "N1", "R1"])
        self.assertEqual(qc[2][1:3], ["true", "removed_in_increment_0001"])
        exclusions = texts["sidecars/exclusions.tsv"].splitlines()
        self.assertEqual([line.split("\t")[0] for line in exclusions[1:]], ["X1", "R1", "A2"])
        self.assertIn("removed_in_increment_0001\tz mis-scaled", exclusions[-1])

    def test_the_folded_derived_manifest_is_the_spliced_order(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root, rows=[row("N1"), row("N2")],
                                    drops=[{"analysis_id": "A1", "reason": "fixture", "defect": False}])
        accept(directory)
        write_texts(root, increment.fold(self.load(), 1))
        materialise_work(root, self.artifacts)
        derived = paths.build_manifest_path(STORE_ID, root=self.artifacts).read_text().splitlines()
        self.assertEqual([line.split("\t")[:2] for line in derived[1:]], [["0", "A2"], ["1", "N1"], ["2", "N2"]])

    def test_a_folded_bundle_passes_the_contract_and_its_fold_is_checked(self) -> None:
        root = write_hybrid_bundle(self.registry)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        self.assertEqual(self.errors(), [])

        text = (root / "analyses.tsv").read_text()
        (root / "analyses.tsv").write_text(text.replace("/data/source/N1.tsv", "/data/source/other.tsv"))
        self.assert_refused("increment 0001 row 'N1' is not folded")

    def test_a_later_increment_may_drop_an_earlier_addition(self) -> None:
        root = write_hybrid_bundle(self.registry)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        accept(write_increment(root, 2, rows=[], drops=[{"analysis_id": "N1", "reason": "fixture", "defect": False}]))
        self.assertEqual(self.errors(), [])
        publish(root, 2, self.artifacts)
        self.assertEqual(self.errors(), [])


class TestTheAmendmentCheck(IncrementTestCase):
    def test_an_open_increment_changes_no_published_membership(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        accept(write_increment(root))
        self.assertEqual(amendment.membership_changes(self.load(), self.artifacts), [])

    def test_an_undeclared_row_is_refused_and_points_to_an_increment_bundle(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        write_texts(root, table_texts([row("A1"), row("A2"), row("X1", excluded="no_build_eligible_rows"),
                                       row("H1")]))
        changes = amendment.membership_changes(self.load(), self.artifacts)
        self.assertEqual(len(changes), 1, changes)
        self.assertIn("analysis 'H1' added", changes[0])
        self.assertIn("Increment Bundle (ADR 0045)", changes[0])

    def test_a_published_increment_with_both_work_files_passes_including_a_later_drop(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        self.assertEqual(amendment.membership_changes(self.load(), self.artifacts), [])
        accept(write_increment(root, 2, rows=[], drops=[{"analysis_id": "N1", "reason": "fixture", "defect": False}]))
        publish(root, 2, self.artifacts)
        self.assertEqual(amendment.membership_changes(self.load(), self.artifacts), [])

    def test_a_fold_whose_exclusions_file_was_not_rewritten_is_refused(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        sidecar = paths.build_manifest_sidecar_path(STORE_ID, root=self.artifacts)
        old_sidecar = sidecar.read_bytes()
        accept(write_increment(root, rows=[], drops=[{"analysis_id": "A1", "reason": "fixture", "defect": False}]))
        publish(root, 1, self.artifacts)
        sidecar.write_bytes(old_sidecar)
        changes = amendment.membership_changes(self.load(), self.artifacts)
        self.assertTrue(any("exclusions" in c for c in changes), changes)

    def test_work_files_no_publication_recorded_are_refused(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        derived = paths.build_manifest_path(STORE_ID, root=self.artifacts)
        derived.write_text(derived.read_text() + "")  # same bytes: still recorded
        self.assertEqual(amendment.membership_changes(self.load(), self.artifacts), [])
        edit_increment(root / "increments" / "0001", published={
            **yaml.safe_load((root / "increments/0001/increment.yaml").read_text())["published"],
            "manifest_sha256": "0" * 64,
        })
        changes = amendment.membership_changes(self.load(), self.artifacts)
        self.assertTrue(any("increment 0001 recorded" in c for c in changes), changes)

    def test_increment_changes_accepts_exactly_the_declared_increment(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        directory = write_increment(root, drops=[{"analysis_id": "A2", "reason": "fixture", "defect": False}])
        self.assertTrue(any("accepted" in c for c in amendment.increment_changes(self.load(), 1, self.artifacts)))
        accept(directory)
        self.assertEqual(amendment.increment_changes(self.load(), 1, self.artifacts), [])

        report = {"added": ["N1"], "dropped": ["A2"], "n_analyses_after": 2}
        self.assertEqual(amendment.increment_changes(self.load(), 1, self.artifacts, splice_report=report), [])
        for wrong, fragment in (
            ({**report, "added": ["N1", "Z9"]}, "added"),
            ({**report, "dropped": []}, "dropped"),
            ({**report, "n_analyses_after": 3}, "n_analyses_after"),
        ):
            with self.subTest(fragment=fragment):
                changes = amendment.increment_changes(self.load(), 1, self.artifacts, splice_report=wrong)
                self.assertTrue(any(fragment in c for c in changes), changes)

    def test_increment_changes_refuses_an_undeclared_change_beside_it(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        accept(write_increment(root))
        text = (root / "analyses.tsv").read_text()
        (root / "analyses.tsv").write_text(text.replace("\t1000\t", "\t1500\t", 1))
        changes = amendment.increment_changes(self.load(), 1, self.artifacts)
        self.assertTrue(any("membership" in c for c in changes), changes)


def increment_candidate(**changes: Any) -> emitter.IncrementCandidate:
    texts = table_texts([row("N1"), row("R1", excluded="ancestry_not_eur")])
    fields: dict[str, Any] = {
        "store_id": STORE_ID, "reason": "fixture increment", "commands": ("fixtures/generate.py --increment",),
        "analyses_tsv": texts["analyses.tsv"],
        "sidecars": {k.removeprefix("sidecars/"): v for k, v in texts.items() if k.startswith("sidecars/")},
        "generator_version": "sha256:" + "b" * 64,
        "source_snapshot": {"inventory_snapshot_id": "fixture-snapshot-v2"},
        "require_added": ("N1",), "created_at": "2026-11-29T00:00:00Z",
        "selection": ("N1", "R1"),
    }
    fields.update(changes)
    return emitter.IncrementCandidate(**fields)


class TestTheEmitsIncrementMode(IncrementTestCase):
    def tree(self, root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_it_writes_only_the_increment_as_a_candidate(self) -> None:
        root = write_hybrid_bundle(self.registry)
        before = self.tree(root)
        emitted = emitter.emit_increment(increment_candidate(), self.registry)
        self.assertEqual(emitted, root / "increments" / "0001")
        after = self.tree(root)
        self.assertEqual({k: v for k, v in after.items() if not k.startswith("increments/")}, before)
        document = yaml.safe_load((emitted / "increment.yaml").read_text())
        self.assertEqual((document["increment"], document["status"]), (1, "candidate"))
        self.assertEqual(document["generator"]["commands"], ["fixtures/generate.py --increment"])
        self.assertEqual(self.errors(), [])
        staging = self.registry / emitter.STAGING_DIRNAME
        self.assertEqual([p.name for p in staging.iterdir() if not p.name.endswith(".lock")], [])

    def test_it_refuses_and_writes_nothing(self) -> None:
        cases = (
            ("status", lambda: write_hybrid_bundle(self.registry, status="accepted"), increment_candidate(),
             "validated"),
            ("open", lambda: write_increment(write_hybrid_bundle(self.registry), rows=[row("N9")]),
             increment_candidate(), "open increment"),
            ("required", lambda: write_hybrid_bundle(self.registry), increment_candidate(require_added=("R1",)),
             "require_added"),
            ("checksum", lambda: write_hybrid_bundle(self.registry),
             increment_candidate(analyses_tsv=table_texts([row("N1", seed="a"), row("R1", excluded="ancestry_not_eur")])
                                 ["analyses.tsv"].replace(row("N1")["checksum"], row("A1")["checksum"])),
             "checksum"),
        )
        for name, setup, candidate, fragment in cases:
            with self.subTest(name):
                shutil.rmtree(self.registry, ignore_errors=True)
                self.registry.mkdir()
                setup()
                before = self.tree(self.registry / STORE_ID)
                with self.assertRaises(emitter.EmitError) as raised:
                    emitter.emit_increment(candidate, self.registry)
                self.assertIn(fragment, str(raised.exception))
                self.assertEqual(self.tree(self.registry / STORE_ID), before)

    def test_the_command_line_emits_a_rendered_increment_and_a_drop_only_one(self) -> None:
        root = write_hybrid_bundle(self.registry)
        rendered = Path(self._tmp.name) / "rendered"
        write_texts(rendered, table_texts([row("N1")]))
        (rendered / "increment.yaml").write_text(yaml.safe_dump({
            "reason": "fixture", "require_added": ["N1"],
            "source_snapshot": {"inventory_snapshot_id": "fixture-snapshot-v2"},
        }))
        selection = write_selection(Path(self._tmp.name) / "selection.tsv", ["N1"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = emitter.main([STORE_ID, "--increment", "--from", str(rendered), "--command", "render fixture",
                                 "--selection", str(selection), "--registry-root", str(self.registry)])
        self.assertEqual(code, 0, err.getvalue())
        accept(root / "increments" / "0001")
        publish(root, 1, self.artifacts)

        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = emitter.main([STORE_ID, "--increment", "--drop", "A1=z mis-scaled", "--reason", "erratum",
                                 "--defect", "--erratum-issue", "opengwas/opengwasdb-stores#532",
                                 "--erratum-text", "A1's z is mis-scaled", "--registry-root", str(self.registry)])
        self.assertEqual(code, 0, err.getvalue())
        document = yaml.safe_load((root / "increments" / "0002" / "increment.yaml").read_text())
        self.assertEqual(document["drops"], [{"analysis_id": "A1", "reason": "z mis-scaled", "defect": True}])
        self.assertEqual(document["erratum"]["issue"], "opengwas/opengwasdb-stores#532")
        self.assertFalse((root / "increments" / "0002" / "analyses.tsv").exists())
        self.assertEqual(self.errors(), [])

    def test_generate_candidate_selects_only_unseen_analyses(self) -> None:
        root = write_hybrid_bundle(self.registry)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        seen = generate_candidate.registered_analysis_ids(self.load())
        self.assertEqual(seen, {"A1", "A2", "X1", "N1", "R1"})


class AttestationTestCase(IncrementTestCase):
    """A published release's records, an open increment, and the writer's command line."""

    def setUp(self) -> None:
        super().setUp()
        self.root = write_hybrid_bundle(self.registry)
        build = yaml.safe_load((self.root / "build.yaml").read_text())
        build["indexed_subsets"] = [{
            "name": "hm3", "resource_id": "hapmap3-hg38", "variant_list": "/data/hm3.txt",
            "reference_assembly": "GRCh38", "checksum": "a" * 64, "options": {"overflow-only": "skip"},
        }]
        (self.root / "build.yaml").write_text(yaml.safe_dump(build))
        materialise_work(self.root, self.artifacts)
        write_increment(self.root)
        records = paths.records_dir(STORE_ID, root=self.artifacts)
        records.mkdir(parents=True)
        self.write_record("build", ["opengwasdb", "build-hybrid", "work/analyses.tsv", "store.partial",
                                    "--variant-reference", "/data/ref/EUR-variants.txt.gz",
                                    "--on-rsid-conflict", "drop", "--source-assembly", "hg38"])
        self.write_record("complete", ["opengwasdb", "complete-hybrid", "a", "b", "--ld-panel", "/data/ld",
                                       "--ancestry", "EUR"])
        self.write_record("completion-indexed-subset-hm3", [
            "opengwasdb", "build-indexed-subset", "store", "--name", "hm3",
            "--variant-list", "/data/hm3.txt", "--overflow-only", "skip"])
        validation = yaml.safe_load((self.root / "validation.yaml").read_text())
        validation["observed"] = {"rsid_conflicts": {"policy": "drop", "n_keys": 1, "n_variants": 1}}
        (self.root / "validation.yaml").write_text(yaml.safe_dump(validation))

    def write_record(self, step: str, argv: list[str]) -> None:
        path = paths.record_path(STORE_ID, step, root=self.artifacts)
        path.write_text(json.dumps({"step": step, "store_id": STORE_ID, "argv": argv, "planned_argv": argv,
                                    "success": True, "start_time": "2026-11-01T00:00:00Z"}))

    def run_writer(self, *extra: str) -> tuple[int, str]:
        err = io.StringIO()
        argv = [STORE_ID, "--increment", "1", "--registry-root", str(self.registry),
                "--artifact-root", str(self.artifacts), *extra]
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = source_record.main(argv)
        return code, err.getvalue()

    ATTESTED = ("--attested-by", "Owner Name", "--variant-reference-sha256", SHA_VR,
                "--ld-panel-digest", "ldpanel:" + "e" * 64)

    def written(self) -> dict[str, Any]:
        return json.loads((self.root / "increments/0001" / increment.SOURCE_RECORD_FILE).read_text())



class TestTheAttestationWriter(AttestationTestCase):
    def test_it_writes_the_attestation_from_the_records_and_the_owners_digests(self) -> None:
        code, err = self.run_writer(*self.ATTESTED)
        self.assertEqual(code, 0, err)
        record = self.written()
        self.assertEqual(record["rsid_conflict_policy"], "drop")
        self.assertEqual(record["source_assembly"], "hg38")
        self.assertIsNone(record["reference_panel_sha256"])
        self.assertEqual(record["variant_reference_sha256"], SHA_VR)
        self.assertEqual(record["indexed_subsets"], {"hm3": {"variant_list_path": "/data/hm3.txt",
                                                              "overflow_only": "skip"}})
        self.assertEqual(record["attested_by"], "Owner Name")
        self.assertIn("owner attestation", record["evidence"]["ld_panel_digest"])
        accept(self.root / "increments/0001")
        self.assertEqual(self.errors(), [])

    def test_it_refuses_without_the_owners_attestation(self) -> None:
        for missing in ("--attested-by", "--variant-reference-sha256", "--ld-panel-digest"):
            with self.subTest(missing=missing):
                index = self.ATTESTED.index(missing)
                args = [*self.ATTESTED[:index], *self.ATTESTED[index + 2:]]
                code, err = self.run_writer(*args)
                self.assertEqual(code, 1)
                self.assertIn(missing, err)
                self.assertFalse((self.root / "increments/0001" / increment.SOURCE_RECORD_FILE).exists())

    def test_it_refuses_records_that_disagree_or_rows_of_another_assembly(self) -> None:
        validation = yaml.safe_load((self.root / "validation.yaml").read_text())
        validation["observed"]["rsid_conflicts"]["policy"] = "refuse"
        (self.root / "validation.yaml").write_text(yaml.safe_dump(validation))
        code, err = self.run_writer(*self.ATTESTED)
        self.assertEqual(code, 1)
        self.assertIn("rsID policy disagrees", err)

        validation["observed"]["rsid_conflicts"]["policy"] = "drop"
        (self.root / "validation.yaml").write_text(yaml.safe_dump(validation))
        derived = paths.build_manifest_path(STORE_ID, root=self.artifacts)
        derived.write_text(derived.read_text().replace("GRCh38", "GRCh37", 1))
        code, err = self.run_writer(*self.ATTESTED)
        self.assertEqual(code, 1)
        self.assertIn("assembly", err)

    def test_a_build_that_took_a_reference_panel_needs_its_attested_digest(self) -> None:
        argv = json.loads(paths.record_path(STORE_ID, "build", root=self.artifacts).read_text())["argv"]
        self.write_record("build", [*argv, "--reference-panel", "/data/panel.tsv"])
        code, err = self.run_writer(*self.ATTESTED)
        self.assertEqual(code, 1)
        self.assertIn("--reference-panel-sha256", err)
        code, err = self.run_writer(*self.ATTESTED, "--reference-panel-sha256", "f" * 64)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.written()["reference_panel_sha256"], "f" * 64)

    def test_it_writes_only_into_a_candidate_increment(self) -> None:
        accept(self.root / "increments/0001")
        code, err = self.run_writer(*self.ATTESTED)
        self.assertEqual(code, 1)
        self.assertIn("candidate", err)


class TestRoundTwoFindings(IncrementTestCase):
    """Review round 2 of #546: owners, loud absence, lifecycle, sidecars, baseline after a rebuild."""

    def test_a_malformed_exclusion_flag_refuses_rather_than_admits(self) -> None:
        root = write_hybrid_bundle(self.registry)
        text = (root / "analyses.tsv").read_text().splitlines()
        text[1] = text[1][: text[1].rfind("\t")] + "\tyes"
        (root / "analyses.tsv").write_text("\n".join(text) + "\n")
        write_increment(root, rows=[], drops=[{"analysis_id": "A1", "reason": "fixture", "defect": False}])
        errors = self.errors()
        self.assertTrue(any(e.startswith("increments") and "exclude_from_build" in e for e in errors), errors)

    def test_the_emit_refuses_by_the_contracts_own_eligibility_predicate(self) -> None:
        write_hybrid_bundle(self.registry, layout="dense")
        with self.assertRaises(emitter.EmitError) as raised:
            emitter.emit_increment(increment_candidate(), self.registry)
        self.assertIn(increment.eligibility_errors(self.load())[0], str(raised.exception))

    def test_a_header_only_analyses_tsv_without_drops_refuses(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        header = (directory / "analyses.tsv").read_text().splitlines()[0]
        (directory / "analyses.tsv").write_text(header + "\n")
        shutil.rmtree(directory / "sidecars")
        self.assert_refused("analyses.tsv has no rows")

    def test_ancestry_rows_with_blank_evidence_refuse(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        (directory / "sidecars" / "ancestry.tsv").write_text("analysis_id\tassigned_ancestry\nN1\tEUR\nR1\t\n")
        self.assert_refused("'R1' has blank ancestry evidence")

    def test_a_source_record_with_empty_evidence_or_malformed_digests_refuses(self) -> None:
        for change, fragment in (
            ({"evidence": {}}, "evidence"),
            ({"variant_reference_sha256": ""}, "variant_reference_sha256"),
            ({"ld_panel_digest": ["a", "b"]}, "ld_panel_digest"),
            ({"reference_panel_sha256": "short"}, "reference_panel_sha256"),
        ):
            with self.subTest(fragment=fragment):
                shutil.rmtree(self.registry / STORE_ID, ignore_errors=True)
                root = write_hybrid_bundle(self.registry)
                directory = write_increment(root)
                (directory / increment.SOURCE_RECORD_FILE).write_text(json.dumps({**SOURCE_RECORD, **change}))
                accept(directory)
                self.assert_refused(fragment)

    def test_a_report_must_list_added_and_dropped(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        accept(write_increment(root))
        for report in ({"dropped": [], "n_analyses_after": 3}, {"added": ["N1"], "n_analyses_after": 3}):
            with self.subTest(report=report):
                changes = amendment.increment_changes(self.load(), 1, self.artifacts, splice_report=report)
                self.assertTrue(any("must list" in c for c in changes), changes)

    def test_an_unreadable_published_declaration_refuses_the_membership_check(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        (root / "increments/0001/increment.yaml").write_text("published: [unclosed\n")
        changes = amendment.membership_changes(self.load(), self.artifacts)
        self.assertTrue(any("increments/0001" in c for c in changes), changes)

    def test_bundle_check_holds_increment_transitions_against_the_previous_bundle(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        previous_registry = Path(self._tmp.name) / "previous"
        shutil.copytree(self.registry, previous_registry)
        previous = bundle.load(STORE_ID, registry_root=previous_registry)
        edit_increment(root / "increments/0001", status="accepted", published=None)
        errors = list(bundle.check(self.load(), previous_status=previous, registry_root=self.registry))
        self.assertTrue(any("'published' to 'accepted'" in e for e in errors), errors)
        shutil.rmtree(root / "increments/0001")
        errors = list(bundle.check(self.load(), previous_status=previous, registry_root=self.registry))
        self.assertTrue(any("increment 0001" in e and "removed" in e for e in errors), errors)

    def test_an_added_row_needs_its_quality_check_and_exclusion_rows(self) -> None:
        root = write_hybrid_bundle(self.registry)
        directory = write_increment(root)
        (directory / "sidecars" / "quality_checks.tsv").unlink()
        self.assert_refused("sidecars/quality_checks.tsv")
        shutil.rmtree(root / "increments")
        directory = write_increment(root)
        (directory / "sidecars" / "exclusions.tsv").unlink()
        self.assert_refused("sidecars/exclusions.tsv")

    def test_folded_rows_keep_increment_order_across_increments(self) -> None:
        root = write_hybrid_bundle(self.registry)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        accept(write_increment(root, 2, rows=[row("N2")]))
        publish(root, 2, self.artifacts)
        self.assertEqual(self.errors(), [])
        lines = (root / "analyses.tsv").read_text().splitlines()
        n1 = next(i for i, line in enumerate(lines) if "\tN1\t" in line)
        n2 = next(i for i, line in enumerate(lines) if "\tN2\t" in line)
        lines[n1], lines[n2] = lines[n2], lines[n1]
        (root / "analyses.tsv").write_text("\n".join(lines) + "\n")
        self.assert_refused("after the rows of every earlier increment")

    def test_source_readiness_stays_with_the_increment(self) -> None:
        root = write_hybrid_bundle(self.registry)
        (root / "sidecars" / "source_readiness.tsv").write_text("analysis_id\treadiness_status\nA1\tok\n")
        directory = write_increment(root)
        (directory / "sidecars" / "source_readiness.tsv").write_text("analysis_id\treadiness_status\nN1\tok\n")
        accept(directory)
        self.assertNotIn("sidecars/source_readiness.tsv", increment.fold(self.load(), 1))

    def test_a_forced_rebuild_starts_the_baseline_again(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        accept(write_increment(root))
        publish(root, 1, self.artifacts)
        rows = [row("A1"), row("A2"), row("X1", excluded="no_build_eligible_rows"), row("N1"),
                row("R1", excluded="ancestry_not_eur"), row("H1")]
        texts = table_texts(rows)
        write_texts(root, texts)
        forced_rebuild(root, self.artifacts)
        self.assertEqual(amendment.membership_changes(self.load(), self.artifacts), [])

    def test_a_hand_edited_reason_on_an_excluded_row_refuses(self) -> None:
        root = write_hybrid_bundle(self.registry)
        materialise_work(root, self.artifacts)
        text = (root / "analyses.tsv").read_text()
        (root / "analyses.tsv").write_text(text.replace("excluded: no_build_eligible_rows: fixture",
                                                        "excluded: no_build_eligible_rows: edited"))
        changes = amendment.membership_changes(self.load(), self.artifacts)
        self.assertTrue(any("'X1' exclusion reason changed" in c for c in changes), changes)

    def test_the_selection_file_is_reconciled_exactly(self) -> None:
        root = write_hybrid_bundle(self.registry)
        before = sorted(p.name for p in root.rglob("*"))
        for listed, fragment in ((["N1"], "not listed"), (["N1", "R1", "M9"], "listed but not")):
            with self.subTest(listed=listed):
                with self.assertRaises(emitter.EmitError) as raised:
                    emitter.emit_increment(increment_candidate(selection=tuple(listed)), self.registry)
                self.assertIn(fragment, str(raised.exception))
                self.assertEqual(sorted(p.name for p in root.rglob("*")), before)
        emitted = emitter.emit_increment(increment_candidate(selection=("N1", "R1")), self.registry)
        document = yaml.safe_load((emitted / "increment.yaml").read_text())
        self.assertEqual(document["selection"]["n"], 2)

    def test_read_selection_takes_the_joiners_shape_and_refuses_blank_or_duplicate_ids(self) -> None:
        path = write_selection(Path(self._tmp.name) / "joiners.tsv", ["GCST1", "GCST2"])
        self.assertEqual(emitter.read_selection(path), ("GCST1", "GCST2"))
        for ids in (["GCST1", "GCST1"], ["GCST1", ""]):
            with self.subTest(ids=ids):
                write_selection(path, ids)
                with self.assertRaises(emitter.EmitError):
                    emitter.read_selection(path)


class TestTheAttestationWriterRoundTwo(AttestationTestCase):
    """Review round 2: the register's atomic writer, and loud absence in the records."""

    def test_it_writes_through_the_registers_atomic_writer(self) -> None:
        from unittest import mock
        from ogstores import register
        with mock.patch.object(register, "write_text_atomically", wraps=register.write_text_atomically) as writer:
            code, err = self.run_writer(*self.ATTESTED)
        self.assertEqual(code, 0, err)
        self.assertEqual(writer.call_count, 1)

    def test_a_blank_source_genome_build_refuses(self) -> None:
        derived = paths.build_manifest_path(STORE_ID, root=self.artifacts)
        derived.write_text(derived.read_text().replace("GRCh38", "", 1))
        code, err = self.run_writer(*self.ATTESTED)
        self.assertEqual(code, 1)
        self.assertIn("blank source_genome_build", err)

    def test_a_declared_indexed_subset_without_its_record_refuses(self) -> None:
        paths.record_path(STORE_ID, "completion-indexed-subset-hm3", root=self.artifacts).unlink()
        code, err = self.run_writer(*self.ATTESTED)
        self.assertEqual(code, 1)
        self.assertIn("completion-indexed-subset-hm3", err)


if __name__ == "__main__":
    unittest.main()
