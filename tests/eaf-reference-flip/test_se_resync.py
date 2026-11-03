#!/usr/bin/env python3
"""The top-hit SE follow-on of the EAF reference flip (`ogstores.eaf_reference_flip`).

A residual SE plane decodes against the frequency of its own cell, and an
imputed cell's frequency is `eaf_reference`.  The flip stores `float32(1) - f`
there, which is not an exact inverse of the earlier `float32(1) - f` for a rare
variant, so the SE an imputed cell decodes to moves in its last bits while each
top-hit tier still holds the SE decoded before the flip.  `validate` compares the
two at `rtol=1e-6, atol=0` and refuses the Store.  The follow-on writes the
tiers' SE again from the decoded plane.

The fixture is `test_eaf_reference_flip.build_fixture(with_se=True)`: a real
tiny Dense 0.2.0 Store with an `int8_residual` SE plane, panel-only variants at
rare frequencies, and top-hit tiers carrying the SE decoded as the core writes it.

Covered: the premise (a flip alone leaves `validate`'s check failing, on imputed
cells of a rare variant only, and the store passed it before); the follow-on,
standalone and as part of a flip, leaving every top-hit SE bit-identical to the
decoded plane with the HM3 subset and every other file untouched and a receipt
with the originals kept; each refusal and guard; `--dry-run`; a Store whose SE
does not depend on the reference; a failed swap, a failed post-swap check and a
kill after each rename.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import zarr
from opengwasdb.encoding import DenseSePlane

import test_eaf_reference_flip as base
from ogstores import eaf_reference_flip, paths

STORE_ID = base.STORE_ID
RESYNC_RECEIPT = "eaf-reference-flip-se-resync.json"


def validate_se_mismatches(store_path: Path) -> dict[str, int]:
    """`validate`'s top-hit SE check: each tier against the decoded plane, rtol 1e-6, atol 0."""
    root = zarr.open_group(store_path / "data.zarr", mode="r")
    plane = DenseSePlane.open(root, base.encoding_of(store_path))
    decoded = plane.band(0, base.N_VARIANTS)
    out: dict[str, int] = {}
    for tier in sorted(root["top_hits"].group_keys()):
        group = root["top_hits"][tier]
        rows = np.asarray(group["variant_index"][:], dtype=np.int64)
        cols = np.asarray(group["analysis_index"][:], dtype=np.int64)
        stored = np.asarray(group["se"][:])
        out[tier] = int((~np.isclose(decoded[rows, cols], stored, rtol=1e-6, atol=0.0, equal_nan=True)).sum())
    return out


def decoded_bits_equal(store_path: Path) -> bool:
    """Every top-hit SE equals the decoded plane bit for bit."""
    root = zarr.open_group(store_path / "data.zarr", mode="r")
    decoded = DenseSePlane.open(root, base.encoding_of(store_path)).band(0, base.N_VARIANTS)
    for tier in root["top_hits"].group_keys():
        group = root["top_hits"][tier]
        rows = np.asarray(group["variant_index"][:], dtype=np.int64)
        cols = np.asarray(group["analysis_index"][:], dtype=np.int64)
        got = np.asarray(group["se"][:], dtype=np.float32).view(np.uint32)
        if not np.array_equal(got, decoded[rows, cols].astype(np.float32).view(np.uint32)):
            return False
    return True


def se_arrays(store_path: Path) -> dict[str, dict[str, str]]:
    return {
        f"top_hits/{tier}/se": base.tree(store_path / "data.zarr" / "top_hits" / tier / "se")
        for tier in ("p_5e_08", "p_5e_06")
    }


class ResyncTestCase(unittest.TestCase):
    """One shared flip-only Store and one shared integrated flip; copies per test."""

    tmp: Path
    library: base.Library

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="ogstores_se_resync_"))
        cls.library = base.Library(cls.tmp / "fixtures")
        pristine, cls.values = cls.library.get(with_se=True)
        cls.pristine_store = pristine / "artifacts" / STORE_ID / "store.opengwasdb"
        cls.pristine_mismatches = validate_se_mismatches(cls.pristine_store)

        # A Store as OGS-00014 stands: flipped, receipt complete, SE not re-synced.
        cls.flipped_root = cls.tmp / "flipped"
        shutil.copytree(pristine, cls.flipped_root)
        with mock.patch.object(eaf_reference_flip, "_resync_apply", return_value=0):
            cls.flip_only = base.run_on(cls.flipped_root, "--workers", "1")
        cls.flipped_store = cls.flipped_root / "artifacts" / STORE_ID / "store.opengwasdb"
        cls.flipped_mismatches = validate_se_mismatches(cls.flipped_store)

        # A flip that runs the follow-on by itself.
        cls.whole_root = cls.tmp / "whole"
        shutil.copytree(pristine, cls.whole_root)
        cls.whole = base.run_on(cls.whole_root, "--workers", "1")
        cls.whole_store = cls.whole_root / "artifacts" / STORE_ID / "store.opengwasdb"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def use_flipped(self) -> None:
        """A private copy of the flipped, not yet re-synced Store."""
        self.root = Path(tempfile.mkdtemp(dir=self.tmp)) / "release"
        shutil.copytree(self.flipped_root, self.root)
        self.artifacts = self.root / "artifacts"
        self.registry = self.root / "stores"
        self.store_path = self.artifacts / STORE_ID / "store.opengwasdb"
        self.side = self.artifacts / STORE_ID

    def argv(self, *extra: str) -> list[str]:
        return [
            STORE_ID,
            "--artifact-root", str(self.artifacts),
            "--registry-root", str(self.registry),
            "--workers", "1",
            *extra,
        ]

    def resync(self, *extra: str) -> tuple[int, str, str]:
        with self.fast_runtime():
            return base.run_main(*self.argv("--resync-se", *extra))

    def fast_runtime(self):
        return mock.patch.object(
            eaf_reference_flip._precedent,
            "_resolve_runtime",
            return_value=(
                base.LOCKED,
                base.engine.identify().version,
            ),
        )

    def receipt(self) -> Path:
        return self.side / "records" / RESYNC_RECEIPT

    def side_tree(self) -> dict[str, str]:
        return base.tree(self.side)

    def assert_refused_unchanged(self, extra: list[str], expected: str, *, resync: bool = True) -> str:
        before = self.side_tree()
        code, out, err = self.resync(*extra) if resync else base.run_main(*self.argv(*extra))
        base.record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn(expected, err)
        self.assertEqual(self.side_tree(), before, "a refusal wrote under the Store Release")
        return err

    # --- the premise ------------------------------------------------------

    def test_the_fixture_passes_validates_check_before_the_flip(self) -> None:
        base.record_check()
        self.assertEqual(self.pristine_mismatches, {"p_5e_08": 0, "p_5e_06": 0})

    def test_a_flip_alone_fails_validates_check_on_rare_imputed_cells_only(self) -> None:
        """The defect: float32 `1 - (1 - f)` is not `f`, and the SE decodes against it."""
        code, out, err = self.flip_only
        base.record_check()
        self.assertEqual(code, 0, out + err)
        self.assertGreater(sum(self.flipped_mismatches.values()), 0, self.flipped_mismatches)
        before = zarr.open_group(self.pristine_store / "data.zarr", mode="r")
        after = zarr.open_group(self.flipped_store / "data.zarr", mode="r")
        for tier in ("p_5e_08", "p_5e_06"):
            group = after["top_hits"][tier]
            rows = np.asarray(group["variant_index"][:], dtype=np.int64)
            cols = np.asarray(group["analysis_index"][:], dtype=np.int64)
            decoded = DenseSePlane.open(after, base.encoding_of(self.flipped_store)).band(0, base.N_VARIANTS)
            bad = ~np.isclose(decoded[rows, cols], np.asarray(group["se"][:]), rtol=1e-6, atol=0.0)
            imputed = np.asarray(group["imputed"][:]).astype(bool)
            panel_before = np.asarray(before["eaf_reference"][:])[rows]
            self.assertFalse((bad & ~imputed).any(), "an observed cell's SE moved")
            self.assertTrue((panel_before[bad] < 0.5).all(), "only a rare original frequency should move")
            # The move is far below the codec's half step.
            rel = np.abs(decoded[rows, cols].astype(np.float64) - np.asarray(group["se"][:])) / np.asarray(group["se"][:])
            self.assertLess(float(rel.max()), 1e-5)

    # --- the follow-on after a flip alone (the OGS-00014 path) ------------

    def test_resync_makes_every_top_hit_se_equal_the_decoded_plane(self) -> None:
        self.use_flipped()
        code, out, err = self.resync()
        base.record_check()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(validate_se_mismatches(self.store_path), {"p_5e_08": 0, "p_5e_06": 0})
        self.assertTrue(decoded_bits_equal(self.store_path))
        self.assertIn("every top-hit SE now equals the decoded plane (rtol 0)", out)

    def test_resync_changes_only_the_top_hit_se_arrays(self) -> None:
        self.use_flipped()
        before_store = base.tree(self.store_path)
        code, out, err = self.resync()
        base.record_check()
        self.assertEqual(code, 0, out + err)
        after_store = base.tree(self.store_path)
        changed = {name for name in set(before_store) | set(after_store) if before_store.get(name) != after_store.get(name)}
        self.assertTrue(changed)
        for name in changed:
            self.assertRegex(name, r"^data\.zarr/top_hits/p_5e_0[68]/se/", name)
        self.assertFalse(any("indexed_subsets" in name for name in changed), "the HM3 subset was touched")

    def test_resync_rewrites_only_rows_that_moved_and_only_imputed_ones(self) -> None:
        self.use_flipped()
        before = zarr.open_group(self.store_path / "data.zarr", mode="r")
        stored = {t: np.asarray(before["top_hits"][t]["se"][:]) for t in ("p_5e_08", "p_5e_06")}
        code, out, err = self.resync()
        base.record_check()
        self.assertEqual(code, 0, out + err)
        after = zarr.open_group(self.store_path / "data.zarr", mode="r")
        for tier, old in stored.items():
            new = np.asarray(after["top_hits"][tier]["se"][:])
            imputed = np.asarray(after["top_hits"][tier]["imputed"][:]).astype(bool)
            moved = old.view(np.uint32) != new.view(np.uint32)
            self.assertTrue(moved.any(), tier)
            self.assertFalse((moved & ~imputed).any(), tier)
            self.assertLess(float((np.abs(new[moved] - old[moved]) / old[moved]).max()), eaf_reference_flip.MAX_RESYNC_REL)

    def test_resync_receipt_and_originals(self) -> None:
        self.use_flipped()
        flip_receipt = self.side / "records" / "eaf-reference-flip.json"
        code, out, err = self.resync()
        base.record_check()
        self.assertEqual(code, 0, out + err)
        record = json.loads(self.receipt().read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["step"], eaf_reference_flip.RESYNC_STEP)
        self.assertEqual(record["flip_receipt"]["sha256"], base._sha256(flip_receipt))
        self.assertEqual(record["check"]["rtol"], 0.0)
        self.assertEqual(record["hm3_sha256_before"], record["hm3_sha256_after"])
        self.assertIsNotNone(record["hm3_sha256_before"])
        replaced = sorted((self.side / "replaced").glob("*-eaf-reference-flip-se-resync"))
        self.assertEqual(len(replaced), 1)
        # The flip's own replaced/ directory is not mistaken for this one.
        self.assertEqual(len(sorted((self.side / "replaced").glob("*-eaf-reference-flip"))), 1)
        for entry in record["arrays"]:
            self.assertGreater(entry["rows_changed"], 0)
            self.assertGreater(entry["validate_se_mismatches_before"], 0)
            original = replaced[0] / entry["array"]
            self.assertEqual(base.tree(original), entry["sha256_before"])
            live = self.store_path / "data.zarr" / entry["array"]
            self.assertEqual(base.tree(live), entry["sha256_after"])
            self.assertEqual((live / "zarr.json").read_bytes(), (original / "zarr.json").read_bytes())
        by_tier = {tier["tier"]: tier for tier in record["check"]["tiers"]}
        for tier, mismatches in self.flipped_mismatches.items():
            self.assertGreaterEqual(by_tier[tier]["rows_changed"], mismatches)
            self.assertEqual(by_tier[tier]["validate_se_mismatches_before"], mismatches)

    def test_dry_run_writes_nothing_and_prints_the_counts(self) -> None:
        self.use_flipped()
        before_store, before_side = base.tree(self.store_path), self.side_tree()
        code, out, err = self.resync("--dry-run")
        base.record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"SE re-sync for {STORE_ID}", out)
        for tier, mismatches in self.flipped_mismatches.items():
            self.assertIn(f"{mismatches} fail validate's rtol", out)
            self.assertIn(f"top_hits/{tier}/se", out)
        self.assertIn("would rewrite 2 array(s)", out)
        self.assertIn("Dry run: nothing in the Store or its records was written.", out)
        self.assertEqual(base.tree(self.store_path), before_store)
        self.assertEqual(self.side_tree(), before_side)

    # --- the follow-on as part of a flip ----------------------------------

    def test_a_flip_resyncs_by_itself(self) -> None:
        code, out, err = self.whole
        base.record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn("SE re-sync for", out)
        self.assertEqual(validate_se_mismatches(self.whole_store), {"p_5e_08": 0, "p_5e_06": 0})
        self.assertTrue(decoded_bits_equal(self.whole_store))
        side = self.whole_root / "artifacts" / STORE_ID
        for name in ("eaf-reference-flip.json", RESYNC_RECEIPT):
            self.assertEqual(json.loads((side / "records" / name).read_text(encoding="utf-8"))["status"], "complete")

    def test_a_whole_flip_and_a_flip_then_resync_agree_byte_for_byte(self) -> None:
        self.use_flipped()
        code, out, err = self.resync()
        base.record_check()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(base.tree(self.store_path), base.tree(self.whole_store))

    def test_a_flip_dry_run_says_the_resync_follows(self) -> None:
        root = Path(tempfile.mkdtemp(dir=self.tmp)) / "release"
        shutil.copytree(self.library.get(with_se=True)[0], root)
        before = base.tree(root / "artifacts" / STORE_ID)
        code, out, err = base.run_on(root, "--dry-run", "--workers", "1")
        base.record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn("re-syncs top_hits/<tier>/se", out)
        self.assertEqual(base.tree(root / "artifacts" / STORE_ID), before)

    def test_a_failed_follow_on_leaves_the_flip_complete_and_says_how_to_finish(self) -> None:
        root = Path(tempfile.mkdtemp(dir=self.tmp)) / "release"
        shutil.copytree(self.library.get(with_se=True)[0], root)
        with mock.patch.dict("os.environ", {eaf_reference_flip.FAIL_AT_ENV: eaf_reference_flip.SE_DECODE_FAULT}):
            code, out, err = base.run_on(root, "--workers", "1")
        base.record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("--resync-se", err)
        side = root / "artifacts" / STORE_ID
        self.assertEqual(json.loads((side / "records" / "eaf-reference-flip.json").read_text(encoding="utf-8"))["status"], "complete")
        self.assertFalse((side / "records" / RESYNC_RECEIPT).exists())
        # ... and the standalone step finishes it.
        code, out, err = base.run_on(root, "--resync-se", "--workers", "1")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(decoded_bits_equal(root / "artifacts" / STORE_ID / "store.opengwasdb"))

    # --- refusals and guards ---------------------------------------------

    def test_an_unflipped_store_is_refused(self) -> None:
        root = Path(tempfile.mkdtemp(dir=self.tmp)) / "release"
        shutil.copytree(self.library.get(with_se=True)[0], root)
        self.root, self.artifacts, self.registry = root, root / "artifacts", root / "stores"
        self.store_path, self.side = self.artifacts / STORE_ID / "store.opengwasdb", self.artifacts / STORE_ID
        self.assert_refused_unchanged([], "has not been flipped by this tool")

    def test_an_incomplete_flip_receipt_is_refused(self) -> None:
        self.use_flipped()
        path = self.side / "records" / "eaf-reference-flip.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = "started"
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assert_refused_unchanged([], "not 'complete'")

    def test_a_live_array_that_disagrees_with_the_flip_receipt_is_refused(self) -> None:
        self.use_flipped()
        array = zarr.open_array(self.store_path / "data.zarr" / "eaf_reference", mode="r+")
        array[3] = np.float32(0.5)
        self.assert_refused_unchanged([], "do not all match the flip receipt")

    def test_a_changed_observed_cell_is_refused(self) -> None:
        """Only an imputed cell's SE depends on eaf_reference; anything else is another defect."""
        self.use_flipped()
        group = zarr.open_group(self.store_path / "data.zarr", mode="r+")
        tier = group["top_hits/p_5e_08"]
        observed = int(np.flatnonzero(np.asarray(tier["imputed"][:]) == 0)[0])
        tier["se"][observed] = tier["se"][observed] * np.float32(1.000001)
        err = self.assert_refused_unchanged([], "observed (imputed == 0) cell(s) would change")
        self.assertIn("nothing was written", err)

    def test_a_change_above_the_relative_limit_is_refused(self) -> None:
        self.use_flipped()
        group = zarr.open_group(self.store_path / "data.zarr", mode="r+")
        tier = group["top_hits/p_5e_08"]
        imputed = int(np.flatnonzero(np.asarray(tier["imputed"][:]) == 1)[0])
        tier["se"][imputed] = tier["se"][imputed] * np.float32(1.01)
        err = self.assert_refused_unchanged([], "relative (limit")
        self.assertIn("nothing was written", err)

    def test_a_second_run_is_refused(self) -> None:
        self.use_flipped()
        code, out, err = self.resync()
        self.assertEqual(code, 0, out + err)
        done = base.tree(self.store_path)
        self.assert_refused_unchanged([], "already exists")
        self.assertEqual(base.tree(self.store_path), done)

    def test_a_store_whose_se_ignores_the_reference_has_nothing_to_resync(self) -> None:
        root = Path(tempfile.mkdtemp(dir=self.tmp)) / "release"
        plain = base.Library(self.tmp / "plain-fixtures").get()[0]
        shutil.copytree(plain, root)
        before = base.tree(root / "artifacts" / STORE_ID)
        with self.fast_runtime():
            code, out, err = base.run_main(
                STORE_ID, "--artifact-root", str(root / "artifacts"),
                "--registry-root", str(root / "stores"), "--resync-se", "--dry-run",
            )
        base.record_check()
        # A float16 Store needs no flip receipt to know there is nothing to do.
        self.assertEqual(code, 0, out + err)
        self.assertIn("does not depend on eaf_reference", out)
        self.assertEqual(base.tree(root / "artifacts" / STORE_ID), before)

    def test_the_decode_fault_refuses_before_writing(self) -> None:
        self.use_flipped()
        with mock.patch.dict("os.environ", {eaf_reference_flip.FAIL_AT_ENV: eaf_reference_flip.SE_DECODE_FAULT}):
            self.assert_refused_unchanged([], "test fault")

    def test_a_staged_verification_failure_swaps_nothing(self) -> None:
        self.use_flipped()
        before = base.tree(self.store_path)
        with mock.patch.dict("os.environ", {eaf_reference_flip.FAIL_AT_ENV: eaf_reference_flip.RESYNC_VERIFY_FAULT}):
            self.assert_refused_unchanged([], "test fault")
        self.assertEqual(base.tree(self.store_path), before)
        self.assertFalse(any(self.store_path.rglob("*.resyncing")), "a staged copy was left behind")

    # --- the checks after the swap, unmocked -------------------------------

    def run_resync_with_spies(self, *, perturb: bool = False, touch_hm3: bool = False):
        """Resync with the real `_verify_live_se`, optionally spoiling what it checks.

        `perturb` flips one bit of the SE the *second* decode returns (the one
        `_verify_live_se` makes); `touch_hm3` appends a byte to an HM3 file right
        after the swap.  The receipt is read back right after `_restore`, before
        a proven restoration deletes it.
        """
        seen: dict = {"verifying": False}
        real_verify = eaf_reference_flip._verify_live_se
        real_decode = eaf_reference_flip._decode_top_hit_se
        real_swap = eaf_reference_flip._swap_all
        real_restore = eaf_reference_flip._restore

        def verify(plan, encoding, workers):
            seen["verifying"] = True
            try:
                return real_verify(plan, encoding, workers)
            finally:
                seen["verifying"] = False

        def decode(*args, **kwargs):
            out = real_decode(*args, **kwargs)
            if perturb and seen["verifying"]:
                out = out.copy()
                out.view(np.uint32)[0] ^= 1
            return out

        def swap(plan, record, record_path, fault):
            real_swap(plan, record, record_path, fault)
            if touch_hm3:
                target = plan.store_path / "data.zarr" / "indexed_subsets" / "hm3" / "variant_index" / "zarr.json"
                with open(target, "ab") as handle:
                    handle.write(b"\n")

        def restore(plan, record, record_path):
            failures = real_restore(plan, record, record_path)
            seen["receipt"] = json.loads(record_path.read_text(encoding="utf-8"))
            return failures

        with mock.patch.object(eaf_reference_flip, "_verify_live_se", side_effect=verify), \
             mock.patch.object(eaf_reference_flip, "_decode_top_hit_se", side_effect=decode), \
             mock.patch.object(eaf_reference_flip, "_swap_all", side_effect=swap), \
             mock.patch.object(eaf_reference_flip, "_restore", side_effect=restore):
            result = self.resync()
        return result, seen

    def test_a_decode_that_disagrees_after_the_swap_restores_the_originals(self) -> None:
        """The rtol-0 half of the post-swap check, real and unmocked."""
        self.use_flipped()
        before_store, before_side = base.tree(self.store_path), self.side_tree()
        (code, out, err), seen = self.run_resync_with_spies(perturb=True)
        base.record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("after the swap, top_hits/", err)
        self.assertIn("differs from the decoded plane", err)
        self.assertIn("Every array is restored", err)
        states = [entry["state"] for entry in seen["receipt"]["replaced"]["arrays"]]
        self.assertEqual(states, ["restored"] * len(states), states)
        self.assertEqual(base.tree(self.store_path), before_store)
        self.assertEqual(self.side_tree(), before_side)

    def test_an_hm3_file_touched_during_the_swap_restores_the_originals(self) -> None:
        """The HM3 half of the post-swap check, real and unmocked."""
        self.use_flipped()
        before_store = base.tree(self.store_path)
        (code, out, err), seen = self.run_resync_with_spies(touch_hm3=True)
        base.record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("the HM3 subset changed during the SE re-sync", err)
        self.assertIn("Every array is restored", err)
        states = [entry["state"] for entry in seen["receipt"]["replaced"]["arrays"]]
        self.assertEqual(states, ["restored"] * len(states), states)
        after = base.tree(self.store_path)
        moved = {name for name in set(before_store) | set(after) if before_store.get(name) != after.get(name)}
        # The originals are back; only the file the test itself touched differs.
        self.assertEqual(moved, {"data.zarr/indexed_subsets/hm3/variant_index/zarr.json"})

    def test_an_fsync_failure_on_a_staged_file_aborts_before_any_rename(self) -> None:
        self.use_flipped()
        before_store, before_side = base.tree(self.store_path), self.side_tree()
        real = eaf_reference_flip._fsync_file

        def flaky(path):
            if ".se.resyncing" in str(path):
                raise OSError("injected staged-file fsync failure")
            return real(path)

        with mock.patch.object(eaf_reference_flip, "_fsync_file", side_effect=flaky), \
             mock.patch.object(eaf_reference_flip, "_rename") as rename:
            code, out, err = self.resync()
        base.record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("injected staged-file fsync failure", err)
        rename.assert_not_called()
        self.assertEqual(base.tree(self.store_path), before_store)
        self.assertEqual(self.side_tree(), before_side)
        self.assertFalse(any(self.store_path.rglob("*.resyncing")), "a staged copy was left behind")

    def test_workers_above_the_326_cap_are_refused(self) -> None:
        for bad in ("17", "0"):
            with self.subTest(workers=bad), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                eaf_reference_flip.parse_args([STORE_ID, "--resync-se", "--workers", bad])
        base.record_check()
        self.assertEqual(eaf_reference_flip.parse_args([STORE_ID, "--workers", "16"]).workers, 16)

    # --- failures after the first rename ----------------------------------

    def test_a_failed_swap_puts_the_originals_back(self) -> None:
        self.use_flipped()
        before_store, before_side = base.tree(self.store_path), self.side_tree()
        real, calls = eaf_reference_flip._rename, {"n": 0}

        def flaky(source, dest):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("injected rename failure")
            return real(source, dest)

        with mock.patch.object(eaf_reference_flip, "_rename", side_effect=flaky):
            code, out, err = self.resync()
        base.record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("Every array is restored", err)
        self.assertEqual(base.tree(self.store_path), before_store)
        self.assertEqual(self.side_tree(), before_side)

    def test_a_failed_check_after_the_swap_restores_the_originals(self) -> None:
        self.use_flipped()
        before_store, before_side = base.tree(self.store_path), self.side_tree()

        def refuse(plan, encoding, workers):
            raise eaf_reference_flip.VerificationFailure("after the swap, injected disagreement")

        with mock.patch.object(eaf_reference_flip, "_verify_live_se", side_effect=refuse):
            code, out, err = self.resync()
        base.record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("injected disagreement", err)
        self.assertEqual(base.tree(self.store_path), before_store)
        self.assertEqual(self.side_tree(), before_side)

    def test_a_kill_after_each_rename_is_described_from_the_filesystem(self) -> None:
        """Killed between the intent states, the next run refuses and says where each file is."""
        for position in range(1, 5):
            with self.subTest(position=position):
                self.use_flipped()
                real, calls = eaf_reference_flip._rename, {"n": 0}

                def kill(source, dest, real=real, calls=calls, position=position):
                    real(source, dest)
                    calls["n"] += 1
                    if calls["n"] == position:
                        raise base.Killed()

                with mock.patch.object(eaf_reference_flip, "_rename", side_effect=kill), \
                     mock.patch.object(eaf_reference_flip, "_restore", side_effect=base.Killed):
                    with self.assertRaises(base.Killed):
                        self.resync()
                record = json.loads(self.receipt().read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "started")
                states = [entry["state"] for entry in record["replaced"]["arrays"]]
                # The receipt announced the rename before it happened, so it is at most one step behind.
                self.assertIn(states[(position - 1) // 2], ("moving_aside", "moved_aside", "swapping_in", "swapped"))
                moved = base.tree(self.store_path)
                code, out, err = self.resync()
                self.assertEqual(code, 1, out + err)
                self.assertIn("already exists", err)
                self.assertIn("read from the filesystem", err)
                self.assertNotIn("No array moved", err)
                if position % 2 == 1:
                    self.assertIn("live missing, original at", err)
                self.assertEqual(base.tree(self.store_path), moved, "the refusal wrote to the Store")
                base.record_check()


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ResyncTestCase))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {base.n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
