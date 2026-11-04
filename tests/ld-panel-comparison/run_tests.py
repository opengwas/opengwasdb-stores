#!/usr/bin/env python3
"""Tests for tools/ld-panel/compare_eur_imputation.py against a completed Store.

The EUR calibration (#348 acceptance item 4) can only be re-checked against a
Reference-Completed Store now, whose `imputed` cells are the panel's own
substitutions. Two things must therefore hold, and this suite pins both:

* an imputed cell is never truth and never training data -- the comparison is
  gated on the Store's `imputed` flag (#348);
* the format-0.2.0 `se` plane is decoded through the core, not read off the raw
  array, where the stored bytes are int8 residuals against the cell's frequency
  rather than standard errors.

The fixture is a real Store, built and then completed by the core's own
entry points in a temporary directory: 600 panel variants on chr22, 400 of them
observed in the Store for two Analyses, so completion imputes 200 x 2 = 400
cells. Every literal asserted here is computed from that construction -- the
frequency table, the standard errors handed to the builder, and the index set
of the observed variants -- never from the code under test.
"""
from __future__ import annotations

import csv
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pysam
import zarr

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "tools" / "ld-panel"

from opengwasdb.build.source import NormalisedAssociation  # noqa: E402
from opengwasdb.layouts.dense.build import build_dense_observed_store  # noqa: E402
from opengwasdb.layouts.dense.complete import complete_dense_store  # noqa: E402
from opengwasdb.variants import CanonicalVariant  # noqa: E402

CHROM = "22"
FIRST_POSITION = 1000
N_VARIANTS = 600
#: The block name the comparison is asked for. It is the half-open span
#: `store_rows` fetches -- `tabix.fetch(chrom, start - 1, end)` -- so a block
#: named `1000-1600` covers positions 1000..1599, which is every variant here.
BLOCK = f"{FIRST_POSITION}-{FIRST_POSITION + N_VARIANTS}"
ANALYSES = ("a", "b")
SEED = 20260806
MASK_FRACTION = 0.2
#: Variant index -> observed in the Store. Two thirds of the panel is observed,
#: so completion has both something to train on and something to fill.
OBSERVED = [index for index in range(N_VARIANTS) if index % 3 != 0]
IMPUTED = [index for index in range(N_VARIANTS) if index % 3 == 0]
#: `SE_RELATIVE_ERROR_BUDGET` in the core's encoding plan: an int8 residual
#: plane is only chosen when every cell it holds decodes within 1% of its value.
SE_RELATIVE_ERROR = 0.01
#: `DEFAULT_Z_SCALE`: the `z` plane is int16 fixed point at 1024.
Z_QUANTUM = 1.0 / 1024.0

checks = 0


def check(condition: bool, message: str) -> None:
    global checks
    checks += 1
    if not condition:
        raise AssertionError(message)


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


comparison = load("compare_eur_imputation")


def alid(index: int) -> str:
    return f"{CHROM}:{FIRST_POSITION + index}:A:G"


def frequencies() -> np.ndarray:
    return np.linspace(0.05, 0.95, N_VARIANTS, dtype=np.float32)


def standard_errors(index: int, column: int) -> float:
    """The SE the fixture hands the builder: the MAF-predicted value, wobbled.

    `log(se)` is linear in `log(2f(1-f))` with one intercept per Analysis, which
    is what makes the core's plan choose an int8 residual plane for a plane this
    small (`SE_EXCEPTION_BUDGET`/`SE_RELATIVE_ERROR_BUDGET`).
    """
    f = float(frequencies()[index])
    return float(
        np.exp(
            (-3.0 + column * 0.2)
            - 0.5 * np.log(2 * f * (1 - f))
            + 0.12 * np.sin(index * (0.07 + column * 0.01))
        )
    )


def z_values() -> np.ndarray:
    """`n_variants x n_analyses` z from the eigenvectors, so imputation succeeds.

    The eigenvector columns are unit-norm, so the coefficients are what carry
    the signal: a z of this shape is 40 v1 + 20 v2 + noise, which the elastic
    net recovers well above the 0.7 quality gate.
    """
    rng = np.random.default_rng(7)
    vectors, _ = np.linalg.qr(rng.normal(size=(N_VARIANTS, 4)))
    noise = np.random.default_rng(11).normal(scale=0.3, size=(N_VARIANTS, len(ANALYSES)))
    signal = 40.0 * vectors[:, [0]] + 20.0 * vectors[:, [1]] + noise
    return signal + np.arange(len(ANALYSES))


def write_panel(panel_root: Path) -> None:
    """Write the EUR panel block the comparison reads: TSV + eigendecomposition."""
    base = panel_root / "EUR" / CHROM / BLOCK
    base.parent.mkdir(parents=True, exist_ok=True)
    fields = ["CHR", "BP", "SNP", "OA", "EA", "EAF", "MAC"]
    with base.with_suffix(".tsv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for index in range(N_VARIANTS):
            f = float(frequencies()[index])
            writer.writerow(
                {"CHR": CHROM, "BP": FIRST_POSITION + index, "SNP": alid(index),
                 "OA": "G", "EA": "A", "EAF": f, "MAC": min(f, 1 - f) * 2000}
            )
    rng = np.random.default_rng(7)
    vectors, _ = np.linalg.qr(rng.normal(size=(N_VARIANTS, 4)))
    np.savez(base.with_suffix(".ldeig.npz"),
             values=np.array([4.0, 2.0, 1.0, 0.5]), vectors=vectors)


def build_store(tmp: Path) -> tuple[Path, Path]:
    """Build the observed-only Store, then complete it.

    Returns both: the Observed-Only source, whose cells are all observed and
    which therefore exercises the "no imputed plane" reading, and the
    Reference-Completed release the calibration is actually re-checked against.
    """
    panel_root = tmp / "panel"
    write_panel(panel_root)
    observed_store = tmp / "observed.opengwasdb"
    completed_store = tmp / "completed.opengwasdb"
    for path in (observed_store, completed_store):
        if path.exists():
            shutil.rmtree(path)
    z = z_values()
    records = [
        NormalisedAssociation(
            analysis_id=analysis_id,
            variant=CanonicalVariant(CHROM, FIRST_POSITION + index, "A", "G"),
            z=float(z[index, column]),
            se=standard_errors(index, column),
            eaf=float(frequencies()[index]),
        )
        for column, analysis_id in enumerate(ANALYSES)
        for index in OBSERVED
    ]
    build_dense_observed_store(
        records, observed_store, store_id="OGS-99999", release_id="OGS-99999",
        reference_assembly="GRCh38",
    )
    complete_dense_store(observed_store, completed_store, panel_root, ancestry="EUR",
                         release_id="OGS-99998")
    return observed_store, completed_store


def store_row_index(store: Path) -> dict[str, int]:
    """ALID -> variant row, read from the Store's own variant index."""
    mapping: dict[str, int] = {}
    with pysam.TabixFile(str(store / "variants.tsv.gz")) as tabix:
        for line in tabix.fetch(CHROM, FIRST_POSITION - 1, FIRST_POSITION + N_VARIANTS):
            fields = line.split("\t")
            mapping[fields[5]] = int(fields[2])
    return mapping


def store_rows_of(store: Path) -> np.ndarray:
    """The Store's own row for each observed fixture variant, in `OBSERVED` order."""
    mapping = store_row_index(store)
    return np.asarray([mapping[alid(index)] for index in OBSERVED], dtype=np.int64)


def task(store: Path, tmp: Path) -> tuple[str, str, str, str, str, str, str, float, float, int]:
    """One comparison task, with the fixture panel standing in for both panels."""
    panel_root = str(tmp / "panel")
    return ("HGDP+1kGP EUR", panel_root, panel_root, panel_root, str(store),
            CHROM, BLOCK, MASK_FRACTION, 0.9, SEED)


def test_the_fixture_holds_imputed_cells(store: Path) -> None:
    # The seam these tests cover only exists if the Store really imputed
    # something: 200 panel-only variants across two Analyses.
    group = zarr.open_group(str(store / "data.zarr"), mode="r")
    imputed = np.asarray(group["imputed"][:])
    check(imputed.shape == (N_VARIANTS, len(ANALYSES)),
          f"unexpected imputed mask shape {imputed.shape}")
    check(int(imputed.sum()) == len(IMPUTED) * len(ANALYSES),
          f"expected {len(IMPUTED) * len(ANALYSES)} imputed cells, got {int(imputed.sum())}")
    check(all(imputed[index].sum() == 0 for index in OBSERVED),
          "an observed variant is flagged imputed")
    check(all(imputed[index].sum() == len(ANALYSES) for index in IMPUTED),
          "a panel-only variant is not flagged imputed")
    manifest = comparison.store_metadata(store)
    check(manifest["format_version"] == "0.2.0",
          f"the fixture is not a format-0.2.0 Store: {manifest['format_version']}")
    check(manifest["completion_state"] == "reference_completed",
          f"the fixture is not Reference-Completed: {manifest['completion_state']}")
    check(manifest["has_imputed_plane"] is True, "the fixture has no imputed plane")
    check(manifest["observed_only"] == comparison.OBSERVED_ONLY_RULE,
          "the sidecar does not record the observed-only rule")
    check(manifest["encoding"]["se"]["kind"] == "int8_residual",
          "the fixture's se plane is not residual-coded, so it does not exercise the decoder")


def test_imputed_cells_are_not_truth_and_not_training(tmp: Path, store: Path) -> None:
    panels = {"UKB EUR": tmp / "panel", "HGDP+1kGP EUR": tmp / "panel"}
    heldout, n_shared = comparison.held_out_alids(
        store, panels, CHROM, BLOCK, MASK_FRACTION, SEED
    )
    observed_alids = {alid(index) for index in OBSERVED}
    # From the fixture's construction: truth is a held-out variant the Store
    # observed; training is every observed variant that is not held out.
    expected_truth = len(heldout & observed_alids)
    expected_training = len(observed_alids - heldout)
    # ... and the counts a Store-agnostic reading would produce, which is what
    # the unrestricted comparison reported.
    unrestricted_truth, unrestricted_training = len(heldout), n_shared - len(heldout)
    check(expected_truth != unrestricted_truth and expected_training != unrestricted_training,
          "the fixture cannot distinguish restricted from unrestricted counts")

    rows = comparison.evaluate(task(store, tmp))
    check(len(rows) == len(ANALYSES), f"expected {len(ANALYSES)} rows, got {len(rows)}")
    for row in rows:
        where = f"analysis {row['analysis_index']}"
        check(row["n_truth"] == expected_truth,
              f"{where}: n_truth {row['n_truth']} != {expected_truth} observed held-out variants")
        check(row["n_training"] == expected_training,
              f"{where}: n_training {row['n_training']} != {expected_training} observed variants")
        check(row["accepted"] is True, f"{where}: the imputation was rejected")
        check(row["n_predicted"] == expected_truth,
              f"{where}: predicted {row['n_predicted']} of {expected_truth} held-out cells")
        check(np.isfinite(row["pearson_r"]) and row["pearson_r"] > 0.9,
              f"{where}: held-out beta correlation {row['pearson_r']} is not the fixture's")


def test_the_se_plane_is_decoded_not_read_raw(store: Path) -> None:
    rows = store_rows_of(store)
    cells = comparison.read_store_cells(store, rows)
    group = zarr.open_group(str(store / "data.zarr"), mode="r")
    raw_se = np.asarray(group["se"].oindex[rows, :])
    raw_z = np.asarray(group["z"].oindex[rows, :])
    columns = np.arange(len(ANALYSES))
    expected_se = np.array([[standard_errors(index, column) for column in columns]
                            for index in OBSERVED], dtype=np.float64)
    expected_z = z_values()[np.ix_(OBSERVED, columns)]

    def failures(got: np.ndarray, want: np.ndarray, allowed: np.ndarray) -> list[str]:
        """Every cell whose decoded value is further from the built one than allowed."""
        return [f"{alid(OBSERVED[row])} analysis {ANALYSES[column]}: {got[row, column]} "
                f"against the built {want[row, column]}"
                for row, column in zip(*np.nonzero(np.abs(got - want) > allowed), strict=True)]

    # What the decoder hands back is what the fixture put in: an int8 residual
    # plane decodes within the plan's 1% budget, and fixed-point z within half a
    # quantum.
    decoded_se = failures(cells.se, expected_se, SE_RELATIVE_ERROR * np.abs(expected_se))
    check(not decoded_se, "decoded SE is not the built SE: " + "; ".join(decoded_se[:3]))
    decoded_z = failures(cells.z, expected_z, Z_QUANTUM)
    check(not decoded_z, "decoded z is not the built z: " + "; ".join(decoded_z[:3]))

    # ... and what the raw array holds is the *codes*, so a raw read would be
    # wrong for essentially every cell. That is the defect this pins.
    check(raw_se.dtype == np.int8 and raw_z.dtype == np.int16,
          f"the fixture's planes are {raw_se.dtype}/{raw_z.dtype}, not int8/int16 codes")
    fixed_point = failures(raw_z / 1024.0, expected_z, Z_QUANTUM)
    check(not fixed_point,
          "the z plane is not int16 fixed point at 1024: " + "; ".join(fixed_point[:3]))
    wrong_if_raw = failures(raw_se.astype(np.float64), expected_se,
                            SE_RELATIVE_ERROR * np.abs(expected_se))
    check(len(wrong_if_raw) == raw_se.size,
          f"only {len(wrong_if_raw)} of {raw_se.size} raw se codes would be wrong to read as SE")

    check(bool(cells.observed.all()), "an observed variant was reported as imputed")
    # An imputed cell decodes to a standard error too, but is not an observation.
    mapping = store_row_index(store)
    imputed_cells = comparison.read_store_cells(
        store, np.asarray([mapping[alid(index)] for index in IMPUTED], dtype=np.int64))
    check(not bool(imputed_cells.observed.any()), "an imputed cell was reported as observed")
    check(bool(np.isfinite(imputed_cells.se).all()),
          "an imputed cell has no decoded standard error")


def test_a_store_without_an_imputed_plane_reads_every_cell(observed: Path,
                                                           completed: Path) -> None:
    """An Observed-Only source is read exactly as before: nothing is excluded.

    The restriction is a property of a release that has an `imputed` plane, not
    of the comparison, so the source of the very same cells must come back with
    every one of them observed -- and with the same decoded values, since
    completion fills missing cells and leaves the observed ones alone.
    """
    # Each Store has its own variant axis -- the completed one is the union of
    # the source's variants and the panel's -- so each is addressed by its own
    # index.
    source = comparison.read_store_cells(
        observed, store_rows_of(observed))
    released = comparison.read_store_cells(
        completed, store_rows_of(completed))
    check(comparison.store_metadata(observed)["has_imputed_plane"] is False,
          "the Observed-Only source has an imputed plane")
    check(comparison.store_metadata(observed)["observed_only"]
          == comparison.NO_IMPUTED_PLANE_RULE,
          "the sidecar does not record that an Observed-Only Store has no imputed cells")
    check(bool(source.observed.all()),
          f"{int((~source.observed).sum())} cells of an Observed-Only Store were excluded")
    check(np.array_equal(source.observed, released.observed),
          "completion changed which of the Store's cells are observed")
    check(np.allclose(source.se, released.se, rtol=SE_RELATIVE_ERROR, equal_nan=True),
          "completion changed an observed cell's standard error")
    check(np.array_equal(source.z, released.z),
          "completion changed an observed cell's z")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        observed, store = build_store(tmp)
        test_the_fixture_holds_imputed_cells(store)
        test_imputed_cells_are_not_truth_and_not_training(tmp, store)
        test_the_se_plane_is_decoded_not_read_raw(store)
        test_a_store_without_an_imputed_plane_reads_every_cell(observed, store)
    print(f"ok — {checks} checks passed")


if __name__ == "__main__":
    main()
