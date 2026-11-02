#!/usr/bin/env python3
"""The Store-side EAF orientation correction (`ogstores.eaf_reference_flip`).

A Reference-Completed Store carries its reference allele frequencies in an
`eaf_reference` plane, and a completing panel that names the other allele leaves
every imputed cell with the complement of the effect-allele frequency. This tool
rewrites that plane in place, once per Store, with an orientation guard before
and after and a receipt of every file's sha256.

The fixture is a real tiny Dense 0.2.0 Reference-Completed Store -- Zarr v3,
sharded, an `int8_residual` `eaf` plane with its baseline and completion mask,
an HM3 Indexed Variant Subset and two top-hit tiers -- written with the pinned
runtime's zarr/numcodecs and read back through the runtime's published reader
(`opengwasdb.encoding.DenseEafPlane`, the plane `StoreQuery` reads).

Cost: each fixture variant is built **once per class** into a pristine tree and
copied per test with `shutil.copytree`, and one completed apply run is shared by
every read-only assertion, so the suite pays for one flip, not one per test.

The fixture's first four variants carry the issue's literal example:
`eaf_reference` `[0.1, 0.25, NaN, 0.8]` must become `[0.9, 0.75, NaN, 0.2]`,
compared as float32 bit patterns (0.2 is the one value a decimal literal cannot
express here: `float32(1) - float32(0.8)` is one ulp below `float32(0.2)`, which
is why the flip is done in float32).

Covered:

* the pass: primary and HM3 flipped, top-hit `eaf` flipped only where
  `imputed == 1` (observed hits bit-identical), a non-canonical NaN payload kept
  bit-exact, every other file under `store.opengwasdb/` byte-identical, a
  complete receipt with per-file sha256 and both r values, originals in
  `replaced/...`;
* every refusal (existing and stale receipts, an already-correct orientation,
  too few comparable variants, Ragged, Zarr v2, Observed-Only, a missing
  completion mask, a top-hit tier without `eaf`/`imputed`, each #326 marker, a
  non-pinned runtime, an unregistered id) exits non-zero and writes nothing;
* the guard boundaries: exactly -0.9 and exactly +0.9 both refuse;
* injected failures: staged verification, the swap fault hook, a mid-swap
  failure (rolled back), and the double rename failure whose original must
  survive in `replaced/` with the receipt at `failed_restoration`;
* `--dry-run` holds the release lock, writes nothing in the Store or its
  records, and prints the counts.
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
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import numpy as np
import zarr
from opengwasdb.encoding import DenseEafPlane, DenseSePlane, StoreEncoding

from ogstores import eaf_reference_flip, release, runtime

STORE_ID = "OGS-00090"

#: The issue's literal example: what a completed Store's eaf_reference holds,
#: and the float32 bit patterns `1 - x` must produce.  0.2 is spelled as its own
#: bit pattern because `float32(1) - float32(0.8)` is 0x3E4CCCCC, one ulp below
#: `float32(0.2)` (0x3E4CCCCD).
LITERAL_BEFORE = [0.1, 0.25, float("nan"), 0.8]
LITERAL_AFTER_BITS = [0x3F666666, 0x3F400000, None, 0x3E4CCCCC]

#: The fixture's shape.  600 variants carry both a finite eaf_reference and
#: observed EAF; 100 are panel-only (imputed in every Analysis) and 100 are
#: observed but off-panel (NaN eaf_reference).
N_VARIANTS = 800
N_ANALYSES = 4
OBSERVED_VARIANTS = 600

#: What the guard can compare: the observed variants with a finite panel
#: frequency.  Variant 2 is the issue's literal NaN, so it is one fewer.
COMPARABLE = OBSERVED_VARIANTS - 1

#: A NaN with a payload the runtime never writes itself, and where it lives.
NAN_PAYLOAD_BITS = 0x7FC00001
NAN_PAYLOAD_ROW = 700

#: How many variants carry no eaf_reference: variant 2 and the off-panel 100.
NAN_REFERENCE = 1 + (N_VARIANTS - 700)

TIERS = {
    "p_5e_08": {"hits": 187, "imputed": 63},
    "p_5e_06": {"hits": 263, "imputed": 101},
}

#: The residual SE plane the `with_se` fixture declares.
SE_ENCODING = {"kind": "int8_residual", "residual_range": 1.0}


def encoding_from_manifest_se() -> StoreEncoding:
    """The `with_se` fixture's encoding plan, for decoding its own planes at build time."""
    return StoreEncoding.from_manifest(
        {
            "version": 3,
            "z": {"kind": "int16_fixed", "scale": 1024},
            "se": SE_ENCODING,
            "eaf": {"kind": "int8_residual", "residual_range": 0.5, "reference": True},
        }
    )


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


def _payload_nan() -> np.float32:
    """A float32 NaN carrying a payload no Python or numpy literal produces."""
    return np.frombuffer(np.uint32(NAN_PAYLOAD_BITS).tobytes(), dtype=np.float32)[0]


def _sharding(*, chunks: tuple[int, ...], shards: tuple[int, ...], dtype: str = "float32", fill: float = 0.0):
    return dict(chunks=chunks, shards=shards, dtype=dtype, fill_value=np.dtype(dtype).type(fill))


@dataclass
class FixtureValues:
    """What the fixture wrote, so a test can state its expectations literally."""

    reference_before: np.ndarray
    observed_value: np.ndarray
    hit_eaf_before: dict[str, np.ndarray] = field(default_factory=dict)
    hit_imputed: dict[str, np.ndarray] = field(default_factory=dict)
    hit_variant: dict[str, np.ndarray] = field(default_factory=dict)
    hit_analysis: dict[str, np.ndarray] = field(default_factory=dict)
    subset: np.ndarray | None = None


def build_fixture(
    root: Path,
    *,
    inverted: bool = True,
    observed_variants: int = OBSERVED_VARIANTS,
    layout: str = "dense",
    completion_state: str = "reference_completed",
    format_version: str = "0.2.0",
    zarr_format: int = 3,
    with_mask: bool = True,
    tier_without: str | None = None,
    with_se: bool = False,
) -> FixtureValues:
    """Build a tiny Dense 0.2.0 Store Release and the values it holds.

    `with_se` gives the Store an `int8_residual` SE plane and top-hit tiers that
    carry `analysis_index` and the SE decoded from it, as the core writes them,
    with panel-only variants at rare frequencies so that a flip's float32
    `1 - f` visibly moves the SE they decode to (`tests/eaf-reference-flip/
    test_se_resync.py`).
    """
    store_path = root / "artifacts" / STORE_ID / "store.opengwasdb"
    data_zarr = store_path / "data.zarr"
    store_path.mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / STORE_ID / "records").mkdir(parents=True, exist_ok=True)
    bundle = root / "stores" / STORE_ID
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "release.yaml").write_text(f"store_id: {STORE_ID}\nstatus: accepted\n", encoding="utf-8")
    rng = np.random.default_rng(11)

    # The stored `eaf_reference` is what the panel wrote: the issue's literal
    # four first (one off-panel variant among them, so its value is NaN), then
    # the complements of a spread of observed frequencies.
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
    if with_se:
        # Panel-only variants: the first half rare (1% to 4%), the rest common.
        # A stored `f` this small has `1 - (1 - f) != f` in float32 for many values.
        reference[observed_variants:650] = rng.uniform(0.011, 0.04, size=650 - observed_variants)
        reference[650:700] = rng.uniform(0.5, 0.99, size=50)
    reference[700:] = np.nan  # off-panel variants carry no panel frequency
    reference[NAN_PAYLOAD_ROW] = _payload_nan()

    imputed = np.zeros((N_VARIANTS, N_ANALYSES), dtype=np.uint8)
    # Variants 600..700 are panel-only: no Analysis observed them, so every cell
    # is imputed and the builder writes them no baseline.  700..800 are observed
    # but off-panel (a NaN eaf_reference), and stay observed.
    imputed[observed_variants:700, :] = 1
    baseline = observed_value.copy()
    baseline[observed_variants:700] = np.nan
    # Observed cells are coded as a zero residual against their own variant's
    # baseline; an imputed cell's stored code is never read.
    codes = np.zeros((N_VARIANTS, N_ANALYSES), dtype=np.int8)
    panel = reference.copy()  # the frequency an imputed hit carries
    observed_rows = np.flatnonzero(~imputed.any(axis=1))
    panel_rows = np.flatnonzero(np.isfinite(reference))
    values = FixtureValues(reference_before=reference, observed_value=observed_value)

    if zarr_format != 3:
        data_zarr.mkdir(parents=True, exist_ok=True)
        (data_zarr / ".zgroup").write_text('{"zarr_format": 2}\n', encoding="utf-8")
    else:
        data_zarr.mkdir(parents=True, exist_ok=True)
        root_group = zarr.open_group(data_zarr, mode="w", zarr_format=3)
        root_group.create_array(
            "eaf", shape=(N_VARIANTS, N_ANALYSES), dtype="int8",
            chunks=(100, N_ANALYSES), shards=(200, 2 * N_ANALYSES), fill_value=np.int8(-128),
        )[:] = codes
        root_group.create_array("eaf_baseline", shape=(N_VARIANTS,), **_sharding(chunks=(100,), shards=(200,)))[:] = baseline
        if with_mask:
            root_group.create_array(
                "imputed", shape=(N_VARIANTS, N_ANALYSES), dtype="uint8",
                chunks=(100, N_ANALYSES), shards=(200, 2 * N_ANALYSES), fill_value=np.uint8(0),
            )[:] = imputed
        root_group.create_array("eaf_reference", shape=(N_VARIANTS,), **_sharding(chunks=(100,), shards=(200,)))[:] = reference
        if with_se:
            # The planes `DenseSePlane` needs: codes, coefficients, an empty exception table, and `z`
            # (whose row chunk the core's top-hit gather reads).  The slope is steep so a
            # one-ulp move of a rare frequency shows in the decoded SE.
            root_group.create_array(
                "z", shape=(N_VARIANTS, N_ANALYSES), dtype="int16",
                chunks=(100, N_ANALYSES), shards=(200, 2 * N_ANALYSES), fill_value=np.int16(-32768),
            )[:] = 0
            se_codes = rng.integers(-12, 13, size=(N_VARIANTS, N_ANALYSES)).astype(np.int8)
            root_group.create_array(
                "se", shape=(N_VARIANTS, N_ANALYSES), dtype="int8",
                chunks=(100, N_ANALYSES), shards=(200, 2 * N_ANALYSES), fill_value=np.int8(-128),
            )[:] = se_codes
            root_group.create_array("se_coefficients", shape=(N_ANALYSES, 2), dtype="float32", chunks=(N_ANALYSES, 2))[:] = (
                np.array([[-3.0, -1.0]] * N_ANALYSES, dtype=np.float32)
            )
            root_group.create_array("se_exception_index", shape=(0,), dtype="int64", chunks=(1,))
            root_group.create_array("se_exception_value", shape=(0,), dtype="float32", chunks=(1,))

        # The HM3 Indexed Variant Subset: a copy of the primary at its own
        # variant indices, which `validate` compares bit for bit.
        subset = np.arange(0, N_VARIANTS, 2, dtype=np.int64)
        hm3 = root_group.require_group("indexed_subsets").require_group("hm3")
        hm3.create_array("variant_index", shape=(len(subset),), dtype="int64", chunks=(64,), shards=(128,))
        hm3["variant_index"][:] = subset
        hm3.create_array("eaf_reference", shape=(len(subset),), **_sharding(chunks=(64,), shards=(128,)))
        hm3["eaf_reference"][:] = reference[subset]
        values.subset = subset

        # Two top-hit tiers: an imputed hit carries the panel's stored frequency,
        # an observed hit the decoded observed one.  An imputed hit sits at a
        # panel variant by construction; neither is possible elsewhere.
        top_hits = root_group.require_group("top_hits")
        for tier, spec in TIERS.items():
            hits = spec["hits"]
            hit_imputed = np.zeros(hits, dtype=np.uint8)
            hit_imputed[: spec["imputed"]] = 1
            rng.shuffle(hit_imputed)
            variant_index = np.empty(hits, dtype=np.int64)
            imputed_hit = hit_imputed.astype(bool)
            if with_se:
                # Each hit is a distinct cell; an imputed one sits where the completion mask says so.
                analysis_index = np.empty(hits, dtype=np.int64)
                imputed_cells = rng.choice((700 - observed_variants) * N_ANALYSES, size=int(imputed_hit.sum()), replace=False)
                variant_index[imputed_hit] = observed_variants + imputed_cells // N_ANALYSES
                analysis_index[imputed_hit] = imputed_cells % N_ANALYSES
                observed_cells = rng.choice(len(observed_rows) * N_ANALYSES, size=int((~imputed_hit).sum()), replace=False)
                variant_index[~imputed_hit] = observed_rows[observed_cells // N_ANALYSES]
                analysis_index[~imputed_hit] = observed_cells % N_ANALYSES
            else:
                variant_index[imputed_hit] = rng.choice(panel_rows, size=int(imputed_hit.sum()), replace=False)
                variant_index[~imputed_hit] = rng.choice(observed_rows, size=int((~imputed_hit).sum()), replace=False)
            eaf = np.where(imputed_hit, panel[variant_index], observed_value[variant_index]).astype(np.float32)
            group = top_hits.require_group(tier)
            group.create_array("variant_index", shape=(hits,), dtype="int64", chunks=(64,), shards=(128,))[:] = variant_index
            if not (tier_without == "imputed" and tier == next(iter(TIERS))):
                group.create_array("imputed", shape=(hits,), dtype="uint8", chunks=(64,), shards=(128,), fill_value=np.uint8(0))[:] = hit_imputed
            if not (tier_without == "eaf" and tier == next(iter(TIERS))):
                group.create_array("eaf", shape=(hits,), **_sharding(chunks=(64,), shards=(128,)))[:] = eaf
            if with_se:
                group.create_array("analysis_index", shape=(hits,), dtype="int64", chunks=(64,), shards=(128,))[:] = analysis_index
                # What the core's top-hit writer stores: the decoded plane, as float32.
                decoded = DenseSePlane.open(root_group, encoding_from_manifest_se()).band(0, N_VARIANTS)
                group.create_array("se", shape=(hits,), **_sharding(chunks=(64,), shards=(128,)))[:] = decoded[
                    variant_index, analysis_index
                ].astype(np.float32)
                values.hit_analysis[tier] = analysis_index
            values.hit_eaf_before[tier] = eaf
            values.hit_imputed[tier] = hit_imputed
            values.hit_variant[tier] = variant_index

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
            "se": SE_ENCODING if with_se else {"kind": "float16"},
            "eaf": {"kind": "int8_residual", "residual_range": 0.5, "reference": True},
        },
    }
    (store_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (store_path / "overview.html").write_text("<html>fixture</html>\n", encoding="utf-8")
    (store_path / "index.sqlite").write_bytes(b"SQLite format 3\x00fixture")
    return values


class Killed(BaseException):
    """Stands in for SIGKILL: nothing may handle it, so no recovery runs."""


class Library:
    """Builds each fixture variant once, then hands out pristine copies of it."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.base.mkdir(parents=True)
        self.cache: dict[tuple, tuple[Path, FixtureValues]] = {}

    def get(self, **variant) -> tuple[Path, FixtureValues]:
        key = tuple(sorted(variant.items()))
        if key not in self.cache:
            root = self.base / f"fixture-{len(self.cache)}"
            self.cache[key] = (root, build_fixture(root, **variant))
        return self.cache[key]


def encoding_of(store_path: Path) -> StoreEncoding:
    manifest = json.loads((store_path / "manifest.json").read_text(encoding="utf-8"))
    return StoreEncoding.from_manifest(manifest["encoding"])


def decoded_observed(store_path: Path) -> np.ndarray:
    """The Store's own observed EAF means, read through the runtime's reader."""
    root = zarr.open_group(store_path / "data.zarr", mode="r")
    read = DenseEafPlane.open(root, encoding_of(store_path)).read_band(0, N_VARIANTS, want_imputed=True)
    values = np.asarray(read.values, dtype=np.float32)
    mask = np.asarray(read.imputed).astype(bool)
    present = ~mask & np.isfinite(values)
    totals = np.where(present, values, np.float32(0.0)).sum(axis=1, dtype=np.float64)
    counts = present.sum(axis=1)
    return np.where(counts > 0, totals / np.maximum(counts, 1), np.float32(np.nan))


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = eaf_reference_flip.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def run_on(root: Path, *extra: str) -> tuple[int, str, str]:
    return run_main(
        STORE_ID,
        "--artifact-root", str(root / "artifacts"),
        "--registry-root", str(root / "stores"),
        *extra,
    )


class FlipTestCase(unittest.TestCase):
    """One shared apply run, and per-test copies of pristine fixture trees."""

    tmp: Path
    library: Library
    values: FixtureValues
    applied_root: Path
    applied: tuple[int, str, str]
    before_tree: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="ogstores_eaf_flip_"))
        cls.library = Library(cls.tmp / "fixtures")
        pristine, cls.values = cls.library.get()
        cls.applied_root = cls.tmp / "applied"
        shutil.copytree(pristine, cls.applied_root)
        cls.before_tree = tree(cls.applied_store_path())
        cls.applied = run_on(cls.applied_root)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def guard_sample(cls):
        """The fixture's guard sample, decoded once: a crash replay reuses it."""
        if "cached_sample" not in cls.__dict__:
            pristine, _ = cls.library.get()
            root = zarr.open_group(
                pristine / "artifacts" / STORE_ID / "store.opengwasdb" / "data.zarr", mode="r"
            )
            cls.cached_sample = eaf_reference_flip._sample_observed(
                root, encoding_of(pristine / "artifacts" / STORE_ID / "store.opengwasdb"),
                eaf_reference_flip.DEFAULT_SAMPLE_SEED,
            )
        return cls.cached_sample

    @classmethod
    def applied_store_path(cls) -> Path:
        return cls.applied_root / "artifacts" / STORE_ID / "store.opengwasdb"

    def setUp(self) -> None:
        self.use()
        self.values = self.library.get()[1]

    def use(self, **variant) -> None:
        """Copy a pristine fixture variant into this test's own directory."""
        pristine, values = self.library.get(**variant)
        self.work = Path(tempfile.mkdtemp(dir=self.tmp))
        dest = self.work / "release"
        self.root = dest
        shutil.copytree(pristine, dest)
        self.registry = dest / "stores"
        self.artifacts = dest / "artifacts"
        self.store_path = self.artifacts / STORE_ID / "store.opengwasdb"
        self.values = values

    # --- helpers ----------------------------------------------------------

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
        code, out, err = run_main(*argv)
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn(expected, err)
        self.assertEqual(self.store_tree(), before_store, "a refusal changed the Store")
        self.assertEqual(self.store_side(), before_side, "a refusal wrote under the Store Release")
        return out, err

    @contextlib.contextmanager
    def fast_runtime(self):
        """Skip the pinned-runtime proof: it has tests of its own and costs a subprocess."""
        with mock.patch.object(
            eaf_reference_flip._precedent,
            "_resolve_runtime",
            return_value=(
                runtime.PINNED_OPENGWASDB_REVISION,
                runtime.PINNED_OPENGWASDB_TAG.removeprefix("v"),
            ),
        ):
            yield

    def kill_run(self, position: int) -> None:
        """Die immediately after the `position`-th rename, as SIGKILL would.

        `_restore` is made to raise the same unrecoverable exception, because a
        killed process runs no handler: the point is to leave the Store and its
        receipt exactly as a SIGKILL between the rename and its receipt update
        would.

        The algebraic verification and the guard's band decode are stubbed for
        these replays only (they have their own tests, and the shared apply
        covers them on a complete run): a crash test replays one staging and
        swap eight times, and re-verifying every array eight times is most of
        what that costs.
        """
        real = eaf_reference_flip._rename
        calls = {"n": 0}

        def kill(source, dest):
            real(source, dest)
            calls["n"] += 1
            if calls["n"] == position:
                raise Killed()

        def verified(target, source, staged, imputed, nan_before):
            return True, nan_before

        with self.fast_runtime(), \
             mock.patch.object(eaf_reference_flip, "_rename", side_effect=kill), \
             mock.patch.object(eaf_reference_flip, "_restore", side_effect=Killed), \
             mock.patch.object(eaf_reference_flip, "_verify_staged", side_effect=verified), \
             mock.patch.object(eaf_reference_flip, "_verify_hm3", return_value=None), \
             mock.patch.object(eaf_reference_flip, "_sample_observed", return_value=self.guard_sample()):
            with self.assertRaises(Killed):
                run_on(self.root)
        self.assertEqual(calls["n"], position, "the kill point was never reached")

    def expected_live_phrases(self, record: dict) -> dict[str, str]:
        """What each array's live state really is, read from the filesystem."""
        hashes = {entry["array"]: entry for entry in record["arrays"]}
        phrases: dict[str, str] = {}
        for entry in record["replaced"]["arrays"]:
            label = entry["array"]
            live = Path(entry["live"])
            recorded = hashes.get(label, {})
            before = recorded.get("sha256_before") or {}
            corrected = recorded.get("sha256_corrected") or recorded.get("sha256_after") or {}
            live_tree = tree(live) if live.exists() else {}
            if not live_tree:
                phrases[label] = "live missing"
            elif before and live_tree == before:
                phrases[label] = "original live"
            elif corrected and live_tree == corrected:
                phrases[label] = "corrected live"
            else:
                phrases[label] = "live present but matches neither recorded state"
        return phrases

    def applied_receipt(self) -> dict:
        path = self.applied_root / "artifacts" / STORE_ID / "records" / "eaf-reference-flip.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def applied_replaced(self, label: str) -> Path:
        roots = sorted((self.applied_root / "artifacts" / STORE_ID / "replaced").glob("*-eaf-reference-flip"))
        self.assertEqual(len(roots), 1, roots)
        return roots[0] / label

    # --- the pass: read-only assertions over one shared apply --------------

    def test_the_shared_apply_succeeded(self) -> None:
        code, out, err = self.applied
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn("now reads r=+", out)

    def test_flips_the_primary_and_hm3_arrays_exactly(self) -> None:
        before = self.values.reference_before
        # The fixture really holds an inverted orientation, read the way the
        # guard reads it (the runtime's own Dense eaf plane).
        observed = decoded_observed(self.store_path)
        ok = np.isfinite(before) & np.isfinite(observed)
        self.assertLess(float(np.corrcoef(before[ok], observed[ok])[0, 1]), -0.99)

        root = zarr.open_group(self.applied_store_path() / "data.zarr", mode="r")
        after = np.asarray(root["eaf_reference"][:])
        record_check()

        # The issue's literal example, as float32 bit patterns.
        bits = after[:4].view(np.uint32)
        self.assertEqual(
            [None if np.isnan(after[i]) else int(bits[i]) for i in range(4)],
            LITERAL_AFTER_BITS,
        )
        # The non-canonical NaN payload survives bit-exact.
        self.assertEqual(int(after[NAN_PAYLOAD_ROW:NAN_PAYLOAD_ROW + 1].view(np.uint32)[0]), NAN_PAYLOAD_BITS)

        # Every finite cell is 1 - x in float32; every non-finite cell is untouched.
        finite = np.isfinite(before)
        np.testing.assert_array_equal(
            after[finite].view(np.uint32), (np.float32(1) - before[finite]).view(np.uint32),
        )
        self.assertTrue(np.array_equal(np.isnan(after), np.isnan(before)))
        np.testing.assert_array_equal(after[~finite].view(np.uint32), before[~finite].view(np.uint32))

        # The HM3 copy equals the flipped primary at its variants.
        hm3_after = np.asarray(root["indexed_subsets/hm3/eaf_reference"][:])
        np.testing.assert_array_equal(
            hm3_after.view(np.uint32), after[self.values.subset].view(np.uint32),
        )

    def test_flips_top_hit_eaf_only_where_imputed(self) -> None:
        root = zarr.open_group(self.applied_store_path() / "data.zarr", mode="r")
        primary = np.asarray(root["eaf_reference"][:])
        for tier in TIERS:
            before = self.values.hit_eaf_before[tier]
            mask = self.values.hit_imputed[tier].astype(bool)
            after = np.asarray(root[f"top_hits/{tier}/eaf"][:])
            record_check()
            np.testing.assert_array_equal(
                after[mask].view(np.uint32), (np.float32(1) - before[mask]).view(np.uint32),
            )
            np.testing.assert_array_equal(after[~mask].view(np.uint32), before[~mask].view(np.uint32))
            np.testing.assert_array_equal(
                after[mask].view(np.uint32),
                primary[self.values.hit_variant[tier][mask]].view(np.uint32),
            )

    def test_only_the_named_array_files_change(self) -> None:
        after = tree(self.applied_store_path())
        record_check()
        touched = ["data.zarr/eaf_reference", "data.zarr/indexed_subsets/hm3/eaf_reference"] + [
            f"data.zarr/top_hits/{tier}/eaf" for tier in TIERS
        ]

        def below_touched(path: str) -> str | None:
            for directory in touched:
                if path.startswith(directory + "/"):
                    return path[len(directory) + 1:]
            return None

        changed = {path for path in set(self.before_tree) | set(after) if self.before_tree.get(path) != after.get(path)}
        self.assertTrue(changed, "nothing changed")
        for path in sorted(changed):
            rest = below_touched(path)
            self.assertIsNotNone(rest, f"an unexpected file changed: {path}")
            self.assertTrue(rest.startswith("c/"), f"metadata changed, not shards: {path}")
        # The manifests and every zarr.json, and everything outside the arrays.
        for path, digest in self.before_tree.items():
            rest = below_touched(path)
            if rest is not None and rest.startswith("c/"):
                continue
            self.assertEqual(after.get(path), digest, f"{path} changed")

    def test_receipt_is_complete_with_hashes_and_r(self) -> None:
        record = self.applied_receipt()
        record_check()
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
        self.assertEqual(primary["rows_flipped"], int(np.isfinite(self.values.reference_before).sum()))
        self.assertEqual(primary["nan_before"], NAN_REFERENCE)
        self.assertEqual(primary["nan_after"], NAN_REFERENCE)
        self.assertTrue(primary["sha256_before"])
        self.assertTrue(primary["sha256_after"])
        self.assertNotEqual(primary["sha256_before"], primary["sha256_after"])
        self.assertIn("c/0", primary["sha256_before"])
        for tier, spec in TIERS.items():
            self.assertEqual(labels[f"top_hits/{tier}/eaf"]["rows_flipped"], spec["imputed"])
        # Every in-flight entry finished swapped.
        self.assertEqual(
            [entry["state"] for entry in record["replaced"]["arrays"]],
            ["swapped"] * len(labels),
        )

    def test_originals_are_kept_outside_the_store(self) -> None:
        replaced = self.applied_replaced("eaf_reference")
        record_check()
        self.assertTrue(replaced.is_dir(), replaced)
        self.assertNotIn("replaced", {p.split("/")[0] for p in tree(self.applied_store_path())})
        original = tree(replaced)
        self.assertEqual(original, {
            path.split("eaf_reference/", 1)[1]: digest
            for path, digest in self.before_tree.items()
            if path.startswith("data.zarr/eaf_reference/")
        })
        self.assertNotEqual(
            tree(self.applied_store_path())["data.zarr/eaf_reference/c/0"], original["c/0"],
        )
        self.assertTrue(self.applied_replaced("top_hits/p_5e_06/eaf").is_dir())
        self.assertTrue(self.applied_replaced("indexed_subsets/hm3/eaf_reference").is_dir())

    # --- receipts ---------------------------------------------------------

    def test_a_second_run_refuses_and_changes_nothing(self) -> None:
        before_store, before_side = tree(self.applied_store_path()), tree(self.applied_root / "artifacts" / STORE_ID)
        code, out, err = run_on(self.applied_root)
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("already exists", err)
        self.assertEqual(tree(self.applied_store_path()), before_store)
        self.assertEqual(tree(self.applied_root / "artifacts" / STORE_ID), before_side)

    def test_an_existing_receipt_refuses(self) -> None:
        self.record_path().parent.mkdir(parents=True, exist_ok=True)
        self.record_path().write_text(json.dumps({"status": "started"}), encoding="utf-8")
        self.assert_refused_unchanged(self.base_argv(), "already exists")

    def test_a_stale_receipt_says_no_array_moved(self) -> None:
        """A receipt whose every array is still pending still refuses -- but says why."""
        self.record_path().parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "status": "started",
            "step": "eaf-reference-flip",
            "store_id": STORE_ID,
            "replaced": {
                "dir": str(self.artifacts / STORE_ID / "replaced" / "20260101T000000Z-eaf-reference-flip"),
                "arrays": [
                    {"array": "eaf_reference", "state": "pending"},
                    {"array": "indexed_subsets/hm3/eaf_reference", "state": "pending"},
                ],
            },
        }
        self.record_path().write_text(json.dumps(receipt), encoding="utf-8")
        out, err = self.assert_refused_unchanged(self.base_argv(), "already exists")
        record_check()
        self.assertIn("No array moved", err)
        self.assertIn(str(self.record_path()), err)
        # A receipt with no recorded hashes is described by presence, not claimed
        # to match anything.
        self.assertIn("records no hashes to compare", err)

    # --- refusals ---------------------------------------------------------

    def test_a_correctly_oriented_store_is_refused(self) -> None:
        self.use(inverted=False)
        self.assert_refused_unchanged(self.base_argv(), "is not inverted")

    def test_too_few_comparable_variants_are_refused(self) -> None:
        self.use(observed_variants=450)
        self.assert_refused_unchanged(self.base_argv(), "comparable variants")

    def test_a_ragged_store_is_refused(self) -> None:
        self.use(layout="ragged")
        self.assert_refused_unchanged(self.base_argv(), "only a 'dense' Store is in scope")

    def test_a_zarr_v2_store_is_refused(self) -> None:
        self.use(zarr_format=2)
        self.assert_refused_unchanged(self.base_argv(), "Zarr v3")

    def test_an_observed_only_store_is_refused(self) -> None:
        self.use(completion_state="observed_only")
        self.assert_refused_unchanged(self.base_argv(), "imputed cells to correct")

    def test_a_manifest_that_is_not_the_current_format_is_refused(self) -> None:
        self.use(format_version="0.1.0")
        self.assert_refused_unchanged(self.base_argv(), "format_version")

    def test_a_store_without_the_completion_mask_is_refused(self) -> None:
        self.use(with_mask=False)
        self.assert_refused_unchanged(self.base_argv(), "imputed` completion mask")

    def test_a_top_hit_tier_without_eaf_is_refused(self) -> None:
        self.use(tier_without="eaf")
        self.assert_refused_unchanged(self.base_argv(), "carries no eaf")

    def test_a_top_hit_tier_without_imputed_is_refused(self) -> None:
        self.use(tier_without="imputed")
        self.assert_refused_unchanged(self.base_argv(), "carries no imputed")

    def test_the_guard_refuses_a_reader_that_returns_no_mask(self) -> None:
        """The mask backstop itself: a plane that reads no mask is not "all observed"."""
        root = mock.MagicMock()
        root.__getitem__.return_value = types.SimpleNamespace(shape=(4,))
        plane = types.SimpleNamespace(
            read_band=lambda r0, r1, want_imputed: types.SimpleNamespace(
                values=np.zeros((r1 - r0, 2), dtype=np.float32), imputed=None
            )
        )
        with mock.patch.object(eaf_reference_flip, "_sampled_bands", return_value=[(0, 4)]), \
             mock.patch.object(eaf_reference_flip.DenseEafPlane, "open", return_value=plane):
            with self.assertRaises(eaf_reference_flip.FlipRefusal) as raised:
                eaf_reference_flip._sample_observed(root, mock.MagicMock(), 1)
        record_check()
        self.assertIn("completion mask", str(raised.exception))

    def test_the_guard_boundary_at_minus_0_9_refuses(self) -> None:
        boundary = eaf_reference_flip.Guard(r=-0.9, n=COMPARABLE, seed=1, bands=4, band_rows=200, sampled=800)
        with mock.patch.object(eaf_reference_flip, "_measure", return_value=boundary):
            self.assert_refused_unchanged(self.base_argv(), "is not inverted")

    def test_the_guard_boundary_at_plus_0_9_refuses(self) -> None:
        inverted = eaf_reference_flip.Guard(r=-1.0, n=COMPARABLE, seed=1, bands=4, band_rows=200, sampled=800)
        boundary = eaf_reference_flip.Guard(r=0.9, n=COMPARABLE, seed=1, bands=4, band_rows=200, sampled=800)
        with mock.patch.object(eaf_reference_flip, "_measure", side_effect=[inverted, boundary]):
            self.assert_refused_unchanged(self.base_argv(), "did not read corrected")

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
        code, out, err = run_main(
            "OGS-00091",
            "--artifact-root", str(self.artifacts),
            "--registry-root", str(self.registry),
        )
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("not a registered Release Bundle", err)

    # --- dry run ----------------------------------------------------------

    def test_dry_run_writes_nothing_and_prints_the_counts(self) -> None:
        before_store, before_side = self.store_tree(), self.store_side()
        code, out, err = run_main(*(self.base_argv() + ["--dry-run"]))
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"eaf-reference-flip for {STORE_ID}", out)
        self.assertIn("would rewrite 4 array(s)", out)
        finite = int(np.isfinite(self.values.reference_before).sum())
        self.assertIn(f"{finite} of {finite} finite row(s) flipped", out)
        self.assertIn(f"n={COMPARABLE} comparable variants", out)
        self.assertIn("r=-", out)
        self.assertIn("r=+", out)
        self.assertIn("(imputed rows only)", out)
        self.assertIn("Dry run: nothing in the Store or its records was written.", out)
        self.assertEqual(self.store_tree(), before_store)
        self.assertEqual(self.store_side(), before_side)

    def test_dry_run_refuses_a_held_lock_without_writing(self) -> None:
        live = release.ReleaseLocks(self.artifacts)
        live.acquire(STORE_ID)
        self.addCleanup(live.release_all)
        before = self.store_tree()
        code, out, err = run_main(*(self.base_argv() + ["--dry-run"]))
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("in progress", err)
        self.assertEqual(self.store_tree(), before)

    def test_dry_run_holds_the_release_lock(self) -> None:
        """The dry run takes the lock, so no writer can change the arrays it measures."""
        seen: dict[str, str | None] = {}
        real = eaf_reference_flip._measure

        def spy(sample, reference):
            if "holder" not in seen:
                seen["holder"] = release.lock_holder(STORE_ID, self.artifacts)
                seen["owns"] = release.ReleaseLocks(self.artifacts).try_acquire(STORE_ID)
            return real(sample, reference)

        with mock.patch.object(eaf_reference_flip, "_measure", side_effect=spy):
            code, out, err = run_main(*(self.base_argv() + ["--dry-run"]))
        record_check()
        self.assertEqual(code, 0, out + err)
        self.assertIsNotNone(seen["holder"], "the dry run held no lock while measuring")
        self.assertIn(f"pid {os.getpid()}", str(seen["holder"]))
        self.assertFalse(seen["owns"], "the dry run's lock was not exclusive")

    # --- injected failures ------------------------------------------------

    def test_a_staged_verification_failure_swaps_nothing(self) -> None:
        before = self.store_tree()
        with self.fast_runtime(), \
             mock.patch.dict(os.environ, {eaf_reference_flip.FAIL_AT_ENV: eaf_reference_flip.VERIFY_FAULT}):
            code, out, err = run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("test fault", err)
        self.assertEqual(self.store_tree(), before, "a failed verification changed the Store")
        self.assertFalse(self.record_path().exists())
        self.assertFalse((self.artifacts / STORE_ID / "replaced").exists())

    def test_the_algebraic_check_rejects_a_verifier_that_would_pass_anything(self) -> None:
        """A verification failure below the fault hook also swaps nothing."""
        before = self.store_tree()
        with self.fast_runtime(), \
             mock.patch.object(
                 eaf_reference_flip,
                 "_verify_staged",
                 side_effect=eaf_reference_flip.VerificationFailure("the staged array is not 1 - x"),
             ):
            code, out, err = run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("not 1 - x", err)
        self.assertEqual(self.store_tree(), before)
        self.assertFalse(self.record_path().exists())
        self.assertFalse((self.artifacts / STORE_ID / "replaced").exists())

    def test_the_swap_fault_hook_moves_nothing(self) -> None:
        before = self.store_tree()
        with self.fast_runtime(), \
             mock.patch.dict(os.environ, {eaf_reference_flip.FAIL_AT_ENV: eaf_reference_flip.SWAP_FAULT}):
            code, out, err = run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("test fault", err)
        self.assertEqual(self.store_tree(), before)
        self.assertFalse(self.record_path().exists())

    def test_a_failed_swap_puts_the_originals_back(self) -> None:
        """A swap that dies part-way leaves the Store byte-identical, and no receipt."""
        before = self.store_tree()
        real = eaf_reference_flip._rename
        calls = {"n": 0}

        def flaky(source, dest):
            calls["n"] += 1
            if calls["n"] == 3:  # the second array's live -> replaced rename
                raise OSError("the filesystem went away")
            return real(source, dest)

        with self.fast_runtime(), mock.patch.object(eaf_reference_flip, "_rename", side_effect=flaky):
            code, out, err = run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"the swap of {STORE_ID} failed", err)
        self.assertIn("Every array is restored", err)
        self.assertEqual(self.store_tree(), before, "a failed swap was not rolled back")
        self.assertFalse(self.record_path().exists())
        self.assertFalse((self.artifacts / STORE_ID / "replaced").exists())

    def test_a_double_rename_failure_keeps_the_original_and_says_so(self) -> None:
        """The staged rename and its restoration both fail: the hardest recovery.

        Nothing may be deleted: the original must survive in `replaced/` and the
        receipt must say `failed_restoration` so the next operator knows why.
        """
        before = self.store_tree()
        real = eaf_reference_flip._rename

        def flaky(source, dest):
            src, dst = Path(source), Path(dest)
            if src.name.startswith(".eaf_reference") and dst.name == "eaf_reference" and dst.parent.name == "data.zarr":
                raise OSError("the staged array could not be renamed into place")
            if "replaced" in src.parts and dst.name == "eaf_reference" and dst.parent.name == "data.zarr":
                raise OSError("the original could not be put back")
            return real(source, dest)

        with self.fast_runtime(), mock.patch.object(eaf_reference_flip, "_rename", side_effect=flaky):
            code, out, err = run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("failed_restoration", err)
        self.assertIn("Recovery could not prove every array restored", err)
        # The original survives, under replaced/, byte for byte.
        originals = sorted((self.artifacts / STORE_ID / "replaced").glob("*-eaf-reference-flip/eaf_reference"))
        self.assertEqual(len(originals), 1, originals)
        self.assertEqual(tree(originals[0]), {
            path.split("data.zarr/eaf_reference/", 1)[1]: digest
            for path, digest in before.items()
            if path.startswith("data.zarr/eaf_reference/")
        })
        # The receipt says what happened, per array.
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed_restoration")
        states = {entry["array"]: entry["state"] for entry in record["replaced"]["arrays"]}
        self.assertEqual(states["eaf_reference"], "failed_restoration")
        self.assertEqual(states["indexed_subsets/hm3/eaf_reference"], "restored")
        # And the message names every array, its original and what is live,
        # read from the filesystem rather than from the receipt's own state.
        for label in states:
            self.assertIn(label, err)
        self.assertIn(f"live missing, original at {originals[0]}", err)
        self.assertIn("hashes to its recorded before-hashes", err)
        self.assertNotIn("No array moved", err)


    # --- SIGKILL at every rename, and the recovery description -------------

    def test_a_kill_at_every_rename_is_described_from_the_filesystem(self) -> None:
        """Killed after each rename, the next run refuses and describes the real state."""
        for position in range(1, 9):
            with self.subTest(position=position):
                self.use()
                self.kill_run(position)
                record = json.loads(self.record_path().read_text(encoding="utf-8"))
                expected = self.expected_live_phrases(record)
                moved = self.store_tree()
                with self.fast_runtime():
                    code, out, err = run_on(self.root)
                self.assertEqual(code, 1, out + err)
                self.assertIn("already exists", err)
                self.assertIn("read from the filesystem", err)
                for label, phrase in expected.items():
                    self.assertIn(f"{label}: {phrase}", err, err)
                # A moved array is never called safe to clear.
                self.assertNotIn("No array moved", err)
                self.assertEqual(self.store_tree(), moved, "the refusal wrote to the Store")
                record_check()

    def test_a_kill_after_one_rename_names_the_displaced_original(self) -> None:
        """Receipt `pending`, live gone, original in `replaced/`: a kill after one rename."""
        self.use()
        self.kill_run(1)
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        original = Path(record["replaced"]["arrays"][0]["original"])
        self.assertTrue(original.is_dir())
        with self.fast_runtime():
            code, out, err = run_on(self.root)
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"live missing, original at {original}", err)
        self.assertIn("hashes to its recorded before-hashes", err)
        self.assertNotIn("No array moved", err)
        self.assertNotIn("Clear the receipt deliberately", err)
        self.assertIn("Do not clear the receipt", err)

    def test_safe_to_clear_advice_appears_only_when_nothing_moved(self) -> None:
        """An untouched Store earns the advice; a moved one does not."""
        # Untouched: a receipt whose arrays are all still where they started.
        self.use()
        record = {
            "status": "started",
            "step": "eaf-reference-flip",
            "store_id": STORE_ID,
            "arrays": [
                {
                    "array": label,
                    "sha256_before": tree(self.store_path / "data.zarr" / label),
                }
                for label in ("eaf_reference", "indexed_subsets/hm3/eaf_reference")
            ],
            "replaced": {
                "dir": str(self.artifacts / STORE_ID / "replaced" / "20260101T000000Z-eaf-reference-flip"),
                "arrays": [
                    {"array": label, "state": "pending"}
                    for label in ("eaf_reference", "indexed_subsets/hm3/eaf_reference")
                ],
            },
        }
        self.record_path().parent.mkdir(parents=True, exist_ok=True)
        self.record_path().write_text(json.dumps(record), encoding="utf-8")
        out, err = self.assert_refused_unchanged(self.base_argv(), "already exists")
        record_check()
        self.assertIn("No array moved", err)
        self.assertIn(f"rm {self.record_path()}", err)
        # And a receipt left by a kill does not: see the test above.

    def test_a_recovery_hash_failure_ends_as_failed_restoration(self) -> None:
        """A hash that cannot be read leaves the evidence and records the failure."""
        self.use()
        real_rename = eaf_reference_flip._rename
        real_sha = eaf_reference_flip._sha256_tree
        state = {"swapped": False}

        def flaky_rename(source, dest):
            src, dst = Path(source), Path(dest)
            if src.name.startswith(".eaf_reference") and dst.name == "eaf_reference" and dst.parent.name == "data.zarr":
                raise OSError("the staged array could not be renamed into place")
            real_rename(source, dest)
            if "replaced" in src.parts and dst.parent.name == "data.zarr":
                state["swapped"] = True

        def flaky_sha(directory):
            target = Path(directory)
            if state["swapped"] and target.name == "eaf_reference" and target.parent.name == "data.zarr":
                raise OSError("the live array could not be read")
            return real_sha(directory)

        with self.fast_runtime(), \
             mock.patch.object(eaf_reference_flip, "_rename", side_effect=flaky_rename), \
             mock.patch.object(eaf_reference_flip, "_sha256_tree", side_effect=flaky_sha):
            code, out, err = run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("failed_restoration", err)
        self.assertIn("cannot hash the live array to prove it restored", err)
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed_restoration")
        states = {entry["array"]: entry["state"] for entry in record["replaced"]["arrays"]}
        self.assertEqual(states["eaf_reference"], "failed_restoration")

    def test_a_durability_failure_keeps_the_evidence(self) -> None:
        """An fsync that fails after a restore keeps the receipt and says so."""
        self.use()
        real_rename = eaf_reference_flip._rename
        real_fsync = eaf_reference_flip._fsync_path
        state = {"swapped": False}

        def flaky_rename(source, dest):
            src, dst = Path(source), Path(dest)
            if src.name.startswith(".eaf_reference") and dst.name == "eaf_reference" and dst.parent.name == "data.zarr":
                raise OSError("the staged array could not be renamed into place")
            real_rename(source, dest)
            if "replaced" in src.parts and dst.parent.name == "data.zarr":
                state["swapped"] = True

        def flaky_fsync(path):
            target = Path(path)
            if state["swapped"] and target.name == "data.zarr":
                raise OSError("the directory could not be fsynced")
            return real_fsync(path)

        with self.fast_runtime(), \
             mock.patch.object(eaf_reference_flip, "_rename", side_effect=flaky_rename), \
             mock.patch.object(eaf_reference_flip, "_fsync_path", side_effect=flaky_fsync):
            code, out, err = run_main(*self.base_argv())
        record_check()
        self.assertEqual(code, 1, out + err)
        self.assertIn("failed_restoration", err)
        self.assertIn("could not be fsynced", err)
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed_restoration")
        # The staged copies and the receipt are still there: nothing was deleted.
        self.assertTrue((self.store_path / "data.zarr" / ".eaf_reference.flipping").is_dir())


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
