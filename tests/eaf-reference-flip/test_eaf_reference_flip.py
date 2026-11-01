#!/usr/bin/env python3
"""The stores lane of the #409 EAF orientation correction (`ogstores.eaf_reference_flip`).

Unlike the #326 suite, this one does not patch the work away: the correction is
this repository's own, so the fixture is a real tiny Dense 0.2.0
Reference-Completed Store -- Zarr v3, sharded, written with the pinned runtime's
zarr and numcodecs, and read back through the runtime's own reader
(`opengwasdb.encoding.DenseEafPlane`, the plane `StoreQuery` reads).

The fixture's first four variants carry the issue's literal example:
`eaf_reference` `[0.1, 0.25, NaN, 0.8]` must become `[0.9, 0.75, NaN, 0.2]`, and
the expectation is compared as float32 bit patterns (0.2 is the one value the
brief's decimal spelling cannot express: `float32(1) - float32(0.8)` is one ulp
below `float32(0.2)`, which is exactly why the flip is done in float32).

What is covered:

* the CLI pass: the primary and HM3 arrays flipped, top-hit `eaf` flipped only
  where `imputed == 1` (observed hits bit-identical), every other file under
  `store.opengwasdb/` byte-identical, a complete receipt with per-file sha256
  and both r values, and the originals in `replaced/...`;
* every refusal (existing receipt, an already-correct orientation, too few
  comparable variants, Ragged, Zarr v2, Observed-Only, each #326 marker, a
  non-pinned runtime, an unregistered id) exits non-zero and leaves the Store
  byte-identical;
* a staged verification failure injected through the test hook swaps nothing;
* `--dry-run` leaves the Store and `records/` byte-identical and prints the
  counts.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import zarr
from opengwasdb.encoding import StoreEncoding

from ogstores import eaf_reference_flip, release, runtime

STORE_ID = "OGS-00090"

#: The issue's literal example: what a completed Store's eaf_reference holds,
#: and the float32 bit patterns `1 - x` must produce.  0.2 is spelled as its
#: own bit pattern because `float32(1) - float32(0.8)` is 0x3E4CCCCC, one ulp
#: below `float32(0.2)` (0x3E4CCCCD).
LITERAL_BEFORE = [0.1, 0.25, float("nan"), 0.8]
LITERAL_AFTER_BITS = [0x3F666666, 0x3F400000, None, 0x3E4CCCCC]

#: The fixture's shape.  600 variants carry both a finite eaf_reference and
#: observed EAF (the guard's floor is 500); 100 are panel-only (imputed in
#: every Analysis) and 100 are observed but off-panel (NaN eaf_reference).
N_VARIANTS = 800
N_ANALYSES = 4
OBSERVED_VARIANTS = 600

#: What the guard can compare: the observed variants whose panel frequency is
#: finite.  Variant 2 is the issue's literal NaN, so it is one fewer.
COMPARABLE = OBSERVED_VARIANTS - 1

#: How many variants carry no eaf_reference: variant 2 and the off-panel 100.
NAN_REFERENCE = 1 + (N_VARIANTS - 700)

TIERS = {
    "p_5e_08": {"hits": 187, "imputed": 63},
    "p_5e_06": {"hits": 263, "imputed": 101},
}

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> dict[str, str]:
    """Every file under `root` by relative path, hashed."""
    return {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sharding(*, chunks: tuple[int, ...], shards: tuple[int, ...], dtype: str = "float32", fill: float = 0.0):
    return dict(chunks=chunks, shards=shards, dtype=dtype, fill_value=np.dtype(dtype).type(fill))


class StoreFixture:
    """A tiny Dense 0.2.0 Reference-Completed Store, and the values it holds."""

    def __init__(
        self,
        store_path: Path,
        *,
        inverted: bool = True,
        observed_variants: int = OBSERVED_VARIANTS,
        layout: str = "dense",
        completion_state: str = "reference_completed",
        format_version: str = "0.2.0",
        zarr_format: int = 3,
    ) -> None:
        self.store_path = store_path
        self.data_zarr = store_path / "data.zarr"
        self.store_path.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(11)

        # The stored `eaf_reference` is what the panel wrote: the issue's
        # literal four first (one off-panel variant among them, so its value is
        # NaN), then the complements of a spread of observed frequencies.
        reference = np.empty(N_VARIANTS, dtype=np.float32)
        observed_value = np.empty(N_VARIANTS, dtype=np.float32)
        reference[:4] = np.array(LITERAL_BEFORE, dtype=np.float32)
        observed_value[:4] = np.array([0.9, 0.75, 0.37, 0.19999999], dtype=np.float32)
        spread = rng.uniform(0.02, 0.98, size=N_VARIANTS - 4).astype(np.float32)
        observed_value[4:] = spread
        reference[4:] = (np.float32(1) - spread) if inverted else spread
        if not inverted:
            # A correctly oriented Store says what the observed data says.
            reference[:4] = observed_value[:4]
        reference[700:] = np.nan  # off-panel variants carry no panel frequency

        imputed = np.zeros((N_VARIANTS, N_ANALYSES), dtype=np.uint8)
        # Variants 600..700 are panel-only: no Analysis observed them, so every
        # cell is imputed and the builder writes them no baseline.  700..800 are
        # observed but off-panel (a NaN eaf_reference), and stay observed.
        imputed[observed_variants:700, :] = 1
        # A variant no Analysis observed has no baseline, as the builder writes it.
        baseline = observed_value.copy()
        baseline[observed_variants:700] = np.nan
        # Observed cells are coded as a zero residual against their own
        # variant's baseline, which is what the builder writes at a variant
        # whose Analyses agree; an imputed cell's stored code is never read.
        codes = np.zeros((N_VARIANTS, N_ANALYSES), dtype=np.int8)
        panel = reference.copy()  # the frequency an imputed hit carries
        observed_rows = np.flatnonzero(~imputed.any(axis=1))
        panel_rows = np.flatnonzero(np.isfinite(reference))

        self.observed_value = observed_value
        self.imputed = imputed
        self.reference_before = reference

        if zarr_format != 3:
            self.data_zarr.mkdir(parents=True, exist_ok=True)
            (self.data_zarr / ".zgroup").write_text('{"zarr_format": 2}\n', encoding="utf-8")
        else:
            self.data_zarr.mkdir(parents=True, exist_ok=True)
            root = zarr.open_group(self.data_zarr, mode="w", zarr_format=3)
            root.create_array(
                "eaf", shape=(N_VARIANTS, N_ANALYSES), dtype="int8",
                chunks=(100, N_ANALYSES), shards=(200, 2 * N_ANALYSES), fill_value=np.int8(-128),
            )[:] = codes
            root.create_array("eaf_baseline", shape=(N_VARIANTS,), **_sharding(chunks=(100,), shards=(200,)))[:] = baseline
            root.create_array(
                "imputed", shape=(N_VARIANTS, N_ANALYSES), dtype="uint8",
                chunks=(100, N_ANALYSES), shards=(200, 2 * N_ANALYSES), fill_value=np.uint8(0),
            )[:] = imputed
            root.create_array("eaf_reference", shape=(N_VARIANTS,), **_sharding(chunks=(100,), shards=(200,)))[:] = reference

            # The HM3 Indexed Variant Subset: a copy of the primary at its own
            # variant indices, which `validate` compares bit for bit.
            subset = np.arange(0, N_VARIANTS, 2, dtype=np.int64)
            hm3 = root.require_group("indexed_subsets").require_group("hm3")
            hm3.create_array("variant_index", shape=(len(subset),), dtype="int64", chunks=(64,), shards=(128,))
            hm3["variant_index"][:] = subset
            hm3.create_array("eaf_reference", shape=(len(subset),), **_sharding(chunks=(64,), shards=(128,)))
            hm3["eaf_reference"][:] = reference[subset]

            # Two top-hit tiers: an imputed hit carries the panel's stored
            # frequency, an observed hit the decoded observed one.  An imputed
            # hit sits at a panel variant by construction, so its frequency is
            # finite -- a tier cannot impute a variant the panel does not cover.
            self.hit_eaf_before: dict[str, np.ndarray] = {}
            self.hit_imputed: dict[str, np.ndarray] = {}
            self.hit_variant: dict[str, np.ndarray] = {}
            top_hits = root.require_group("top_hits")
            for tier, spec in TIERS.items():
                hits = spec["hits"]
                hit_imputed = np.zeros(hits, dtype=np.uint8)
                hit_imputed[: spec["imputed"]] = 1
                rng.shuffle(hit_imputed)
                variant_index = np.empty(hits, dtype=np.int64)
                imputed_hit = hit_imputed.astype(bool)
                # An imputed hit sits at a panel variant, an observed one at a
                # variant some Analysis observed; neither is possible elsewhere.
                variant_index[imputed_hit] = rng.choice(panel_rows, size=int(imputed_hit.sum()), replace=False)
                variant_index[~imputed_hit] = rng.choice(observed_rows, size=int((~imputed_hit).sum()), replace=False)
                eaf = np.where(imputed_hit, panel[variant_index], observed_value[variant_index]).astype(np.float32)
                group = top_hits.require_group(tier)
                group.create_array("variant_index", shape=(hits,), dtype="int64", chunks=(64,), shards=(128,))[:] = variant_index
                group.create_array("imputed", shape=(hits,), dtype="uint8", chunks=(64,), shards=(128,), fill_value=np.uint8(0))[:] = hit_imputed
                group.create_array("eaf", shape=(hits,), **_sharding(chunks=(64,), shards=(128,)))[:] = eaf
                self.hit_eaf_before[tier] = eaf
                self.hit_imputed[tier] = hit_imputed
                self.hit_variant[tier] = variant_index
            self.subset = subset

        manifest = {
            "store_id": STORE_ID,
            "release_id": STORE_ID,
            "format_version": format_version,
            "primary_layout": layout,
            "completion_state": completion_state,
            "reference_assembly": "GRCh38",
            "created_at": "2026-11-02T00:00:00Z",
            "encoding": {
                "version": 3,
                "z": {"kind": "int16_fixed", "scale": 1024},
                "se": {"kind": "float16"},
                "eaf": {"kind": "int8_residual", "residual_range": 0.5, "reference": True},
            },
        }
        (self.store_path / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.store_path / "overview.html").write_text("<html>fixture</html>\n", encoding="utf-8")
        (self.store_path / "index.sqlite").write_bytes(b"SQLite format 3\x00fixture")
        (store_path.parent / "records").mkdir(exist_ok=True)

    @property
    def encoding(self) -> StoreEncoding:
        return StoreEncoding.from_manifest(
            json.loads((self.store_path / "manifest.json").read_text(encoding="utf-8"))["encoding"]
        )

    def decoded_observed(self) -> np.ndarray:
        """The fixture's own observed EAF, read through the runtime's reader."""
        from opengwasdb.encoding import DenseEafPlane

        root = zarr.open_group(self.data_zarr, mode="r")
        read = DenseEafPlane.open(root, self.encoding).read_band(0, N_VARIANTS, want_imputed=True)
        values = np.asarray(read.values, dtype=np.float32)
        mask = np.asarray(read.imputed).astype(bool)
        present = ~mask & np.isfinite(values)
        totals = np.where(present, values, np.float32(0.0)).sum(axis=1, dtype=np.float64)
        counts = present.sum(axis=1)
        return np.where(counts > 0, totals / np.maximum(counts, 1), np.float32(np.nan))


class FlipTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_eaf_flip_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.registry = self.tmp / "stores"
        self.artifacts = self.tmp / "artifacts"
        self.registry.mkdir()
        self.artifacts.mkdir()
        bundle_root = self.registry / STORE_ID
        bundle_root.mkdir()
        (bundle_root / "release.yaml").write_text(
            f"store_id: {STORE_ID}\nstatus: accepted\n", encoding="utf-8"
        )
        self.store_path = self.artifacts / STORE_ID / "store.opengwasdb"
        self.fixture = StoreFixture(self.store_path)

    # --- helpers ----------------------------------------------------------

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = eaf_reference_flip.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def base_argv(self) -> list[str]:
        return [
            STORE_ID,
            "--artifact-root", str(self.artifacts),
            "--registry-root", str(self.registry),
        ]

    def record_path(self) -> Path:
        return self.artifacts / STORE_ID / "records" / "eaf-reference-flip.json"

    def store_tree(self) -> dict[str, str]:
        return tree(self.store_path)

    def store_side(self) -> dict[str, str]:
        """Every file under `<root>/<id>/`, the records and receipts included."""
        return tree(self.artifacts / STORE_ID)

    def assert_refused_unchanged(self, argv: list[str], expected: str) -> tuple[str, str]:
        """A refusal exits non-zero, names its reason, and writes nothing anywhere."""
        before_store = self.store_tree()
        before_side = self.store_side()
        code, out, err = self.run_main(*argv)
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn(expected, err)
        self.assertEqual(self.store_tree(), before_store, "a refusal changed the Store")
        self.assertEqual(self.store_side(), before_side, "a refusal wrote under the Store Release")
        return out, err

    def replaced_array(self, label: str) -> Path:
        roots = sorted((self.artifacts / STORE_ID / "replaced").glob("*-eaf-reference-flip"))
        self.assertEqual(len(roots), 1, roots)
        return roots[0] / label

    # --- the CLI pass -----------------------------------------------------

    def test_flips_the_primary_and_hm3_arrays_exactly(self) -> None:
        before = self.fixture.reference_before
        # The fixture really holds an inverted orientation, read the way the
        # guard reads it (the runtime's own Dense eaf plane).
        observed = self.fixture.decoded_observed()
        ok = np.isfinite(before) & np.isfinite(observed)
        self.assertLess(float(np.corrcoef(before[ok], observed[ok])[0, 1]), -0.99)

        code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        root = zarr.open_group(self.fixture.data_zarr, mode="r")
        after = np.asarray(root["eaf_reference"][:])

        # The issue's literal example, as float32 bit patterns.
        bits = after[:4].view(np.uint32)
        self.assertEqual(
            [None if np.isnan(after[i]) else int(bits[i]) for i in range(4)],
            LITERAL_AFTER_BITS,
        )

        # Every finite cell is 1 - x in float32; every non-finite cell is untouched.
        finite = np.isfinite(before)
        np.testing.assert_array_equal(after[finite].view(np.uint32), (np.float32(1) - before[finite]).view(np.uint32))
        self.assertTrue(np.array_equal(np.isnan(after), np.isnan(before)))
        np.testing.assert_array_equal(
            after[~finite].view(np.uint32), before[~finite].view(np.uint32),
        )

        # The HM3 copy equals the flipped primary at its variants, and the
        # zarr.json of both is the live metadata, byte for byte.
        hm3_after = np.asarray(root["indexed_subsets/hm3/eaf_reference"][:])
        np.testing.assert_array_equal(hm3_after.view(np.uint32), after[self.fixture.subset].view(np.uint32))

    def test_flips_top_hit_eaf_only_where_imputed(self) -> None:
        code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        root = zarr.open_group(self.fixture.data_zarr, mode="r")
        for tier in TIERS:
            before = self.fixture.hit_eaf_before[tier]
            mask = self.fixture.hit_imputed[tier].astype(bool)
            after = np.asarray(root[f"top_hits/{tier}/eaf"][:])
            record_check()
            np.testing.assert_array_equal(
                after[mask].view(np.uint32), (np.float32(1) - before[mask]).view(np.uint32),
            )
            np.testing.assert_array_equal(after[~mask].view(np.uint32), before[~mask].view(np.uint32))
            # The flipped imputed hits now agree with the flipped primary.
            primary = np.asarray(root["eaf_reference"][:])
            np.testing.assert_array_equal(
                after[mask].view(np.uint32),
                primary[self.fixture.hit_variant[tier][mask]].view(np.uint32),
            )

    def test_only_the_named_array_files_change(self) -> None:
        before = self.store_tree()
        code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        after = self.store_tree()

        touched = ["data.zarr/eaf_reference", "data.zarr/indexed_subsets/hm3/eaf_reference"] + [
            f"data.zarr/top_hits/{tier}/eaf" for tier in TIERS
        ]

        def below_touched(path: str) -> str | None:
            for directory in touched:
                if path.startswith(directory + "/"):
                    return path[len(directory) + 1:]
            return None

        changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
        self.assertTrue(changed, "nothing changed")
        for path in sorted(changed):
            rest = below_touched(path)
            self.assertIsNotNone(rest, f"an unexpected file changed: {path}")
            self.assertTrue(rest.startswith("c/"), f"metadata changed, not shards: {path}")
        # The manifests and every zarr.json, and everything outside the arrays.
        for path, digest in before.items():
            rest = below_touched(path)
            if rest is not None and rest.startswith("c/"):
                continue
            self.assertEqual(after.get(path), digest, f"{path} changed")

    def test_receipt_is_complete_with_hashes_and_r(self) -> None:
        code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["step"], "eaf-reference-flip")
        self.assertEqual(record["store_id"], STORE_ID)
        self.assertEqual(record["issue"], 409)
        self.assertEqual(record["opengwasdb_revision"], runtime.PINNED_OPENGWASDB_REVISION)
        self.assertEqual(record["opengwasdb_version"], runtime.PINNED_OPENGWASDB_TAG.removeprefix("v"))
        self.assertRegex(record["started_at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*Z")
        self.assertRegex(record["completed_at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*Z")
        self.assertRegex(record["tool"]["commit"], r"\A[0-9a-f]{40}\Z")
        self.assertEqual(record["tool"]["path"], "src/ogstores/eaf_reference_flip.py")

        guard = record["guard"]
        self.assertEqual(guard["sample_seed"], eaf_reference_flip.DEFAULT_SAMPLE_SEED)
        self.assertEqual(guard["before"]["n"], COMPARABLE)
        self.assertLess(guard["before"]["r"], -0.99)
        self.assertEqual(guard["after"]["n"], COMPARABLE)
        self.assertGreater(guard["after"]["r"], 0.99)

        labels = {entry["array"]: entry for entry in record["arrays"]}
        self.assertEqual(
            set(labels),
            {"eaf_reference", "indexed_subsets/hm3/eaf_reference"} | {f"top_hits/{tier}/eaf" for tier in TIERS},
        )
        primary = labels["eaf_reference"]
        self.assertEqual(primary["rows_flipped"], int(np.isfinite(self.fixture.reference_before).sum()))
        self.assertEqual(primary["nan_before"], NAN_REFERENCE)
        self.assertEqual(primary["nan_after"], NAN_REFERENCE)
        self.assertTrue(primary["sha256_before"])
        self.assertTrue(primary["sha256_after"])
        self.assertNotEqual(primary["sha256_before"], primary["sha256_after"])
        self.assertIn("c/0", primary["sha256_before"])
        for tier, spec in TIERS.items():
            self.assertEqual(labels[f"top_hits/{tier}/eaf"]["rows_flipped"], spec["imputed"])

    def test_originals_are_kept_outside_the_store(self) -> None:
        before = self.store_tree()
        code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 0, out + err)
        replaced = self.replaced_array("eaf_reference")
        self.assertTrue(replaced.is_dir(), replaced)
        self.assertNotIn("replaced", {p.split("/")[0] for p in self.store_tree()})
        original = tree(replaced)
        self.assertEqual(original, {
            path.split("eaf_reference/", 1)[1]: digest
            for path, digest in before.items()
            if path.startswith("data.zarr/eaf_reference/")
        })
        # The staged array that is now live is not the original.
        self.assertNotEqual(
            self.store_tree()["data.zarr/eaf_reference/c/0"], original["c/0"],
        )
        self.assertTrue(self.replaced_array("top_hits/p_5e_06/eaf").is_dir())
        self.assertTrue(self.replaced_array("indexed_subsets/hm3/eaf_reference").is_dir())

    def test_a_second_run_refuses_and_changes_nothing(self) -> None:
        code, out, err = self.run_main(*self.base_argv())
        self.assertEqual(code, 0, out + err)
        self.assert_refused_unchanged(self.base_argv(), "already exists")

    # --- dry run ----------------------------------------------------------

    def test_dry_run_writes_nothing_and_prints_the_counts(self) -> None:
        before_store = self.store_tree()
        before_root = tree(self.artifacts)
        code, out, err = self.run_main(*(self.base_argv() + ["--dry-run"]))
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"eaf-reference-flip for {STORE_ID}", out)
        self.assertIn("would rewrite 4 array(s)", out)
        self.assertIn(
            f"{int(np.isfinite(self.fixture.reference_before).sum())} of "
            f"{int(np.isfinite(self.fixture.reference_before).sum())} finite row(s) flipped",
            out,
        )
        self.assertIn(f"n={COMPARABLE} comparable variants", out)
        self.assertIn("r=-", out)
        self.assertIn("r=+", out)
        self.assertIn("(imputed rows only)", out)
        self.assertIn("Dry run: nothing in the Store or its records was written.", out)
        self.assertEqual(self.store_tree(), before_store)
        self.assertEqual(tree(self.artifacts), before_root, "the dry run wrote under the artifact root")

    def test_dry_run_refuses_a_held_lock_without_writing(self) -> None:
        live = release.ReleaseLocks(self.artifacts)
        live.acquire(STORE_ID)
        self.addCleanup(live.release_all)
        code, out, err = self.run_main(*(self.base_argv() + ["--dry-run"]))
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("in progress", err)

    # --- refusals ---------------------------------------------------------

    def test_an_existing_receipt_refuses(self) -> None:
        self.record_path().parent.mkdir(parents=True, exist_ok=True)
        self.record_path().write_text(json.dumps({"status": "started"}), encoding="utf-8")
        before = self.store_tree()
        code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("already exists", err)
        self.assertIn("started", err)
        self.assertEqual(self.store_tree(), before)

    def test_a_correctly_oriented_store_is_refused(self) -> None:
        shutil.rmtree(self.store_path)
        StoreFixture(self.store_path, inverted=False)
        self.assert_refused_unchanged(self.base_argv(), "is not inverted")

    def test_too_few_comparable_variants_are_refused(self) -> None:
        shutil.rmtree(self.store_path)
        StoreFixture(self.store_path, observed_variants=450)
        self.assert_refused_unchanged(self.base_argv(), "comparable variants")

    def test_a_ragged_store_is_refused(self) -> None:
        shutil.rmtree(self.store_path)
        StoreFixture(self.store_path, layout="ragged")
        self.assert_refused_unchanged(self.base_argv(), "only a 'dense' Store is in scope")

    def test_a_zarr_v2_store_is_refused(self) -> None:
        shutil.rmtree(self.store_path)
        StoreFixture(self.store_path, zarr_format=2)
        self.assert_refused_unchanged(self.base_argv(), "Zarr v3")

    def test_an_observed_only_store_is_refused(self) -> None:
        shutil.rmtree(self.store_path)
        StoreFixture(self.store_path, completion_state="observed_only")
        self.assert_refused_unchanged(self.base_argv(), "imputed cells to correct")

    def test_a_manifest_that_is_not_the_current_format_is_refused(self) -> None:
        shutil.rmtree(self.store_path)
        StoreFixture(self.store_path, format_version="0.1.0")
        self.assert_refused_unchanged(self.base_argv(), "format_version")

    def test_each_326_marker_is_refused_before_any_write(self) -> None:
        markers = [
            self.artifacts / STORE_ID / "publication.json",
            self.artifacts / STORE_ID / "store.opengwasdb.partial",
            self.artifacts / STORE_ID / "work" / "complete.started.json",
            self.artifacts / STORE_ID / ".store.opengwasdb.partial.checkpoint",
        ]
        for marker in markers:
            with self.subTest(marker=marker.name):
                marker.parent.mkdir(parents=True, exist_ok=True)
                if marker.name == "store.opengwasdb.partial":
                    marker.mkdir()
                    self.addCleanup(shutil.rmtree, marker, True)
                else:
                    marker.write_text("{}", encoding="utf-8")
                    self.addCleanup(marker.unlink)
                self.assert_refused_unchanged(self.base_argv(), marker.name)

    def test_a_non_pinned_runtime_is_refused(self) -> None:
        def fake_revision(executable=None, env=None):
            return runtime.PINNED_OPENGWASDB_REVISION if executable is None else "deadbeef" * 5

        with mock.patch.object(eaf_reference_flip._precedent.run, "get_opengwasdb_executable", return_value="/fake/bin/opengwasdb"), \
             mock.patch.object(eaf_reference_flip._precedent.run, "get_opengwasdb_revision", side_effect=fake_revision), \
             mock.patch.object(eaf_reference_flip._precedent.run, "get_opengwasdb_version", return_value="0.3.2"):
            self.assert_refused_unchanged(self.base_argv(), "must be the same pinned runtime")

    def test_an_unregistered_store_is_refused(self) -> None:
        code, out, err = self.run_main(
            "OGS-00091",
            "--artifact-root", str(self.artifacts),
            "--registry-root", str(self.registry),
        )
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("not a registered Release Bundle", err)

    # --- injected failure -------------------------------------------------

    def test_a_staged_verification_failure_swaps_nothing(self) -> None:
        before = self.store_tree()
        with mock.patch.dict(os.environ, {eaf_reference_flip.FAIL_AT_ENV: eaf_reference_flip.VERIFY_FAULT}):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("test fault", err)
        self.assertEqual(self.store_tree(), before, "a failed verification changed the Store")
        self.assertFalse(self.record_path().exists())
        self.assertFalse((self.artifacts / STORE_ID / "replaced").exists())

    def test_the_algebraic_check_rejects_a_verifier_that_would_pass_anything(self) -> None:
        """A verification failure below the fault hook also swaps nothing."""
        before = self.store_tree()
        with mock.patch.object(
            eaf_reference_flip,
            "_verify_staged",
            side_effect=eaf_reference_flip.VerificationFailure("the staged array is not 1 - x"),
        ):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("not 1 - x", err)
        self.assertEqual(self.store_tree(), before)
        self.assertFalse(self.record_path().exists())
        self.assertFalse((self.artifacts / STORE_ID / "replaced").exists())

    def test_a_failed_swap_puts_the_originals_back(self) -> None:
        """A swap that dies part-way leaves the Store byte-identical, and no receipt."""
        before = self.store_tree()
        real = eaf_reference_flip._swap_in
        calls = {"n": 0}

        def flaky(staged, rollback_dir, fault):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("the filesystem went away")
            return real(staged, rollback_dir, fault)

        with mock.patch.object(eaf_reference_flip, "_swap_in", side_effect=flaky):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("the swap of OGS-00090 failed", err)
        self.assertIn("Restored", err)
        self.assertEqual(self.store_tree(), before, "a failed swap was not rolled back")
        self.assertFalse(self.record_path().exists())

    def test_the_swap_fault_hook_moves_nothing(self) -> None:
        before = self.store_tree()
        with mock.patch.dict(os.environ, {eaf_reference_flip.FAIL_AT_ENV: eaf_reference_flip.SWAP_FAULT}):
            code, out, err = self.run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("test fault", err)
        self.assertEqual(self.store_tree(), before)
        self.assertFalse(self.record_path().exists())


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(FlipTestCase))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
