"""One-off, value-changing in-place correction of a Store's EAF orientation (#409).

The HGDP+1kGP panel some completed Stores were built against carries the EAF of
the *other* allele, so `_orient_row` wrote `1 - f` into `eaf_reference` for
every panel variant.  A Dense Store's imputed cells read that plane at decode
time, so every imputed cell served the complement of the effect-allele
frequency.  The owner's decision (2026-11-01, issue #409) is to rewrite the
affected Store in place, once, and to record it as a Release Erratum: the
values change, so this is not the physical-representation-only exception of
#326 (opengwasdb#326 / `ogstores.se_reencode`).

The arrays that hold the wrong orientation, and nothing else:

* `data.zarr/eaf_reference`            -- every finite value becomes `1 - x`;
* `data.zarr/indexed_subsets/hm3/eaf_reference`
                                       -- the same, when the Indexed Variant
                                          Subset is present (it is a copy of the
                                          primary restricted to its variants);
* `data.zarr/top_hits/<tier>/eaf`      -- the `eaf` of *imputed* hits only
                                          (`imputed == 1`); an observed hit's
                                          `eaf` is the decoded observed value
                                          and is left bit-identical.

Observed `eaf`, `eaf_baseline`, `z` and the stored SE codes are untouched:
finishing is a global allele flip whose only in-store consequence is those three
arrays -- and, in a Store whose SE is an `int8_residual` plane, the decoded SE of
imputed cells, which the follow-on (see `--resync-se` below) brings the top-hit
tiers back in line with.

    pixi run eaf-reference-flip OGS-00014 --dry-run
    pixi run eaf-reference-flip OGS-00014

Safety, in the order it happens:

1. The release lock is taken (never waited for) -- by `--dry-run` too -- after
   the same refusals `se_reencode` makes: a registered id, a non-symlinked
   Store, no publication or completion in progress, and the imported *and* PATH
   `opengwasdb` both the commit `pixi.lock` resolves.  A receipt that already
   exists refuses: `1 - (1 - x)` is `x`, so a second run silently restores the
   bug.
2. Only a Dense, format-0.2.0, Zarr-v3, `reference_completed` Store is in
   scope.  Ragged, Hybrid, Zarr v2 and Observed-Only Stores are refused with
   the reason, never skipped; so is a top-hit tier without `eaf` or `imputed`
   and a Store without its completion mask.
3. **Before touching anything**, the orientation guard: on a deterministic
   sample of variants that carry both a finite `eaf_reference` and an observed
   EAF in at least one Analysis, the Pearson r between the two must be `< -0.9`
   over at least 500 variants.  Observed EAF is decoded through the runtime's
   own reader (`opengwasdb.encoding.DenseEafPlane`, the plane `StoreQuery`
   reads), never re-derived here.  The seed, r and n are recorded.
4. Each target is staged as a sibling `.<name>.flipping` directory: the live
   `zarr.json` is copied verbatim and whole shards are written through that
   metadata, so codecs and chunking cannot drift.  `float32(1) - x` is
   computed in float32 (the same operation on the same input is bit-identical
   everywhere, which is what `validate` requires of the copies) and only where
   the value is finite; a NaN keeps its exact payload.
5. The staged arrays are verified algebraically against the originals --
   `new == 1 - old` on finite cells as raw bit patterns, NaN positions and
   payloads unchanged, filtered rows bit-identical, and the HM3 copy equal to
   the staged primary at its variants.  The guard is then repeated on the
   staged primary and must read `r > 0.9`.  Only then is anything renamed.
6. The swap is `store/swap.py`'s pattern at directory level: the original array
   directory is moved into `<store-dir>/replaced/<UTC>-eaf-reference-flip/`
   (never inside the Store), then the staged directory is moved into its name.
   The receipt carries each array's progress -- `pending`, `moved_aside`,
   `swapped`, `restored` -- and is fsynced before and after every rename, so a
   process killed between two renames leaves a readable account of which file
   is where.  A failed run puts every original back, proves it by hashing the
   live path against the recorded before-hashes, and only then removes the
   receipt and `replaced/`.  Anything less restored is left in place, the
   receipt is marked `failed_restoration`, and the run exits non-zero naming
   each array, its original and what is live: the evidence is never destroyed.
7. `records/eaf-reference-flip.json` is written `started` immediately before
   the first rename and `complete` after the last: per-touched-array sha256 of
   every file before and after, the flipped/NaN counts, the guard before and
   after, the core revision and the tool revision (the shape
   `tools/relocate_store.py` uses).

`--dry-run` performs steps 1-5 into a scratch directory outside the Store,
prints the plan and the counts, and changes nothing in the Store or its
records.  The tool never touches a Store-root file (`manifest.json`,
`index.sqlite`, `overview.html`, ...) or any array it does not name.

`--resync-se` runs only the follow-on, on a Store this tool already flipped; a
flip of a residual-SE Store runs it by itself after the swap.  See
`docs/eaf-reference-flip.md`, "The SE coupling".
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
import zarr
from opengwasdb.encoding import DenseEafPlane, StoreEncoding

from ogstores import paths, release, run
from ogstores import se_reencode as _precedent

#: The step name under `records/`, and so the receipt's file name.
RECORD_STEP: str = "eaf-reference-flip"

#: The tool's own revision, recorded in the receipt (`relocate_store`'s shape).
TOOL_PATH: str = "src/ogstores/eaf_reference_flip.py"

#: The suffix a staged array directory carries inside its own group, on the
#: same filesystem so the swap is two `os.replace` renames.
STAGING_SUFFIX: str = ".flipping"

#: The stamp suffix `replaced/` uses (`<UTC stamp>-eaf-reference-flip`).
REPLACED_SUFFIX: str = "eaf-reference-flip"

#: The receipt's per-array progress states, in the order a run passes through
#: them.  The three `-ing` states are written *before* the rename they announce,
#: so a process killed mid-rename has already said what it was doing; the state
#: after the rename is written once the directory metadata is fsynced.  The
#: filesystem, not the receipt, decides what is described to an operator.
PENDING: str = "pending"
MOVING_ASIDE: str = "moving_aside"
MOVED_ASIDE: str = "moved_aside"
SWAPPING_IN: str = "swapping_in"
SWAPPED: str = "swapped"
RESTORING: str = "restoring"
RESTORED: str = "restored"
FAILED_RESTORATION: str = "failed_restoration"

#: The guard's deterministic sample: bands of consecutive variants, `MAX_BANDS`
#: of them at most, so a 30M-variant Store costs a bounded number of band
#: decodes (the exploration behind #409 used 60 bands of 200 rows).
BAND_ROWS: int = 200
MAX_BANDS: int = 60

#: The issue's evidence floor: fewer comparable variants than this cannot show
#: the orientation at all, and a run must not guess.
MIN_COMPARABLE: int = 500

#: An inverted Store reads strongly negative; a corrected one strongly
#: positive.  Strict, as the issue states it: equality at the boundary is not
#: evidence of anything and refuses.
BEFORE_MAX_R: float = -0.9
AFTER_MIN_R: float = 0.9

#: The default sample seed.  Recorded in the receipt, overridable so a re-run of
#: the evidence can draw another sample.
DEFAULT_SAMPLE_SEED: int = 20261101

#: Test-only fault points, after `run.fault_boundary` and relocate_store's
#: variables: raise at a named step so a test can prove what happens when a
#: rename fails.
FAIL_AT_ENV: str = "OGSTORES_TEST_EAF_REFERENCE_FLIP_FAIL_AT"
VERIFY_FAULT, SWAP_FAULT = "verify", "swap"

#: The Dense / 0.2.0 / reference-completed values this tool accepts.  Literal
#: strings, not the runtime's enums: the manifest is read before the pinned
#: runtime is trusted with anything, and a wrong value must read as one.
DENSE_LAYOUT: str = "dense"
REFERENCE_COMPLETED: str = "reference_completed"
FORMAT_VERSION: str = "0.2.0"
ZARR_FORMAT_V3: int = 3


class FlipRefusal(RuntimeError):
    """The command refuses to run; nothing of the Store was written."""


class VerificationFailure(FlipRefusal):
    """A staged array did not verify; nothing was swapped."""


def _tool_commit() -> str | None:
    """The checkout's HEAD, or None when git cannot answer (relocate_store's shape)."""
    result = subprocess.run(
        ["git", "-C", str(paths.REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _fault_at() -> str | None:
    """The test-only fault point this process was started with, if any."""
    return os.environ.get(FAIL_AT_ENV) or None


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One array the correction rewrites, and whether the imputed mask filters it."""

    group: str
    array: str
    imputed_filter: bool = False

    @property
    def label(self) -> str:
        return f"{self.group}/{self.array}" if self.group else self.array


def _read_manifest(store_path: Path) -> dict[str, Any]:
    manifest_path = store_path / "manifest.json"
    if not manifest_path.is_file():
        raise FlipRefusal(f"no Store Manifest at {manifest_path}; {store_path} is not a Store Release")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FlipRefusal(f"cannot read the Store Manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise FlipRefusal(f"the Store Manifest {manifest_path} is not a JSON object")
    return manifest


def _refuse_out_of_scope(store_path: Path, manifest: dict[str, Any]) -> None:
    """Refuse any Store this correction does not implement, naming what was found.

    Every check is a *refusal* rather than a skip: a Ragged or Observed-Only
    Store that this tool quietly passed over would look corrected and not be.
    """
    layout = manifest.get("primary_layout")
    if layout != DENSE_LAYOUT:
        raise FlipRefusal(
            f"this Store's primary_layout is {layout!r}; only a {DENSE_LAYOUT!r} Store is in "
            "scope (a Ragged or Hybrid Store holds its reference EAF elsewhere)"
        )
    state = manifest.get("completion_state")
    if state != REFERENCE_COMPLETED:
        raise FlipRefusal(
            f"this Store's completion_state is {state!r}; only a {REFERENCE_COMPLETED!r} "
            "Store carries imputed cells to correct"
        )
    version = manifest.get("format_version")
    if version != FORMAT_VERSION:
        raise FlipRefusal(
            f"this Store's format_version is {version!r}; this correction is written "
            f"against {FORMAT_VERSION} only"
        )
    zarr_json = store_path / "data.zarr" / "zarr.json"
    if not zarr_json.is_file():
        raise FlipRefusal(
            f"{store_path / 'data.zarr'} carries no zarr.json; this correction rewrites "
            "Zarr v3 metadata and cannot touch a v2 Store"
        )
    try:
        group_meta = json.loads(zarr_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FlipRefusal(f"cannot read the Zarr metadata {zarr_json}: {exc}") from exc
    if int(group_meta.get("zarr_format", -1)) != ZARR_FORMAT_V3:
        raise FlipRefusal(
            f"{zarr_json} declares zarr_format {group_meta.get('zarr_format')!r}; this "
            "correction is written against Zarr v3 (format 0.2.0) only"
        )


def _targets(data_zarr: Path) -> list[Target]:
    """The arrays to rewrite, taken from the Store's own layout.

    The primary plane is required; the HM3 copy and the top-hit tiers are
    taken when present.  A missing `eaf_reference` is a refusal, not an empty
    plan: a reference-completed Store that does not carry the plane is not the
    Store this issue describes.  A top-hit tier that exists but lacks `eaf` or
    `imputed` is refused rather than skipped -- an uncorrected imputed hit is
    exactly the defect, and skipping the tier would leave it in place.
    """
    root = zarr.open_group(data_zarr, mode="r")
    if "eaf_reference" not in root:
        raise FlipRefusal(
            f"{data_zarr} carries no eaf_reference array; a reference-completed "
            "Store's imputed cells read that plane, so its absence is a different defect"
        )
    targets = [Target(group="", array="eaf_reference")]
    if "indexed_subsets" in root and "hm3" in root["indexed_subsets"]:
        hm3 = root["indexed_subsets/hm3"]
        if "eaf_reference" in hm3:
            targets.append(Target(group="indexed_subsets/hm3", array="eaf_reference"))
    if "top_hits" in root:
        for threshold in sorted(root["top_hits"].group_keys()):
            tier = root["top_hits"][threshold]
            missing = [name for name in ("eaf", "imputed") if name not in tier]
            if missing:
                raise FlipRefusal(
                    f"data.zarr/top_hits/{threshold} carries no {' or '.join(missing)}; a "
                    "top-hit tier this correction cannot read is not skipped, because its "
                    "imputed hits would keep the wrong orientation"
                )
            targets.append(Target(group=f"top_hits/{threshold}", array="eaf", imputed_filter=True))
    return targets


# ---------------------------------------------------------------------------
# The orientation guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Guard:
    """One orientation measurement: r, the variants behind it, and the sample."""

    r: float | None
    n: int
    seed: int
    bands: int
    band_rows: int
    sampled: int

    def describe(self) -> str:
        r = "nan" if self.r is None else f"{self.r:+.4f}"
        return (
            f"r={r} over n={self.n} comparable variants "
            f"({self.bands} bands x {self.band_rows} rows, {self.sampled} sampled, seed {self.seed})"
        )


def _pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    """Pearson r, or None when either side is constant (an undefined r)."""
    if len(x) < 2:
        return None
    dx = x - x.mean()
    dy = y - y.mean()
    sxx = float(np.dot(dx, dx))
    syy = float(np.dot(dy, dy))
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return float(np.dot(dx, dy) / np.sqrt(sxx * syy))


@dataclass(frozen=True)
class _Sample:
    """The sampled variants' observed EAF means, and where they came from."""

    rows: np.ndarray
    observed: np.ndarray
    bands: int
    band_rows: int
    seed: int

    @property
    def sampled(self) -> int:
        return int(len(self.rows))


def _sampled_bands(n_variants: int, seed: int) -> list[tuple[int, int]]:
    """Deterministic, non-overlapping row bands: the guard's sample."""
    band_rows = min(BAND_ROWS, n_variants)
    if band_rows <= 0:
        return []
    available = n_variants // band_rows
    rng = np.random.default_rng(seed)
    chosen = np.sort(rng.permutation(available)[: min(MAX_BANDS, available)])
    return [(int(b) * band_rows, int(b) * band_rows + band_rows) for b in chosen]


def _sample_observed(root: Any, encoding: StoreEncoding, seed: int) -> _Sample:
    """Decode each sampled variant's mean observed EAF through the runtime reader.

    `DenseEafPlane.read_band` is the plane `StoreQuery` reads: it applies the
    declared encoding and substitutes the panel's frequency on imputed cells.
    Those cells are dropped here by the plane's own mask, so what remains is
    the observed EAF and nothing re-derived from `eaf_reference`.  A completed
    Store without that mask is refused: treating every cell as observed would
    make the guard measure the panel against itself.
    """
    n_variants = int(root["eaf_reference"].shape[0])
    try:
        plane = DenseEafPlane.open(root, encoding)
    except Exception as exc:
        raise FlipRefusal(
            f"the runtime reader cannot open this Store's observed EAF plane: {exc}"
        ) from exc
    rows: list[np.ndarray] = []
    observed: list[np.ndarray] = []
    bands = _sampled_bands(n_variants, seed)
    band_rows = min(BAND_ROWS, n_variants)
    for r0, r1 in bands:
        try:
            read = plane.read_band(r0, r1, want_imputed=True)
        except Exception as exc:
            raise FlipRefusal(
                f"the runtime reader cannot decode rows {r0}:{r1} of this Store's "
                f"observed EAF plane: {exc}"
            ) from exc
        if read.imputed is None:
            raise FlipRefusal(
                "this Store reads no completion mask (`imputed`), so which cells are "
                "observed cannot be known and the orientation guard cannot run. A "
                "reference-completed Store must carry one"
            )
        values = np.asarray(read.values, dtype=np.float32)
        mask = np.asarray(read.imputed).astype(bool)
        present = ~mask & np.isfinite(values)
        totals = np.where(present, values, np.float32(0.0)).sum(axis=1, dtype=np.float64)
        counts = present.sum(axis=1)
        means = np.where(counts > 0, totals / np.maximum(counts, 1), np.nan)
        rows.append(np.arange(r0, r1, dtype=np.int64))
        observed.append(means)
    if not rows:
        return _Sample(np.empty(0, np.int64), np.empty(0, np.float64), 0, band_rows, seed)
    return _Sample(
        rows=np.concatenate(rows),
        observed=np.concatenate(observed),
        bands=len(bands),
        band_rows=band_rows,
        seed=seed,
    )


def _measure(sample: _Sample, reference: Any) -> Guard:
    """Correlate a sampled variant set's observed EAF with a reference plane."""
    if sample.sampled == 0:
        return Guard(r=None, n=0, seed=sample.seed, bands=0, band_rows=sample.band_rows, sampled=0)
    ref = np.asarray(reference.oindex[sample.rows], dtype=np.float32)
    ok = np.isfinite(ref) & np.isfinite(sample.observed)
    x = ref[ok].astype(np.float64)
    y = np.asarray(sample.observed[ok], dtype=np.float64)
    return Guard(
        r=_pearson(x, y),
        n=int(ok.sum()),
        seed=sample.seed,
        bands=sample.bands,
        band_rows=sample.band_rows,
        sampled=sample.sampled,
    )


def _require_inverted(guard: Guard) -> None:
    """The pre-condition: the Store must actually read inverted, over enough variants."""
    if guard.n < MIN_COMPARABLE:
        raise FlipRefusal(
            f"the orientation guard found only {guard.n} comparable variants "
            f"(finite eaf_reference and an observed EAF in at least one Analysis); "
            f"at least {MIN_COMPARABLE} are needed to show the orientation ({guard.describe()})"
        )
    if guard.r is None or not guard.r < BEFORE_MAX_R:
        raise FlipRefusal(
            "the orientation guard is not inverted: expected r < "
            f"{BEFORE_MAX_R} between eaf_reference and the observed EAF, got {guard.describe()}. "
            "This Store does not need (or has already had) this correction."
        )


def _require_corrected(guard: Guard) -> None:
    """The post-condition, on the staged primary: the flip must have worked."""
    if guard.r is None or not guard.r > AFTER_MIN_R:
        raise VerificationFailure(
            "the orientation guard on the staged primary did not read corrected: "
            f"expected r > {AFTER_MIN_R}, got {guard.describe()}"
        )


# ---------------------------------------------------------------------------
# Hashing, staging, verification
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_tree(directory: Path) -> dict[str, str]:
    """Every regular file under `directory`, by relative path, hashed."""
    if not directory.is_dir():
        return {}
    return {
        str(path.relative_to(directory)): _sha256_file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _flip(values: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
    """`float32(1) - x` on finite cells, unchanged everywhere else.

    The complement is taken in float32 -- the plane's own dtype -- so the same
    operation on the same input is bit-identical for every copy of it, which is
    what `validate` compares (`eaf_consistent`, the HM3 copy).  Routing through
    float64 would send `0.99` back as `0.010000000707...` and break that.  A
    non-finite cell is passed through untouched, NaN payload included.
    """
    flipped = np.where(np.isfinite(values), np.float32(1) - values, values)
    if mask is None:
        return np.ascontiguousarray(flipped, dtype=values.dtype)
    return np.ascontiguousarray(np.where(mask, flipped, values), dtype=values.dtype)


def _shard_rows(array: Any) -> int:
    """The rows one stored shard holds: the write must cover whole shards."""
    shards = getattr(array, "shards", None)
    if shards:
        return int(shards[0])
    return int(array.chunks[0])


def _slice_rows(n_rows: int, step: int) -> Iterator[tuple[int, int]]:
    for start in range(0, n_rows, step):
        yield start, min(start + step, n_rows)


@dataclass
class StagedArray:
    """One staged array: what was written, how it verified, and its hashes."""

    target: Target
    staged_dir: Path
    live_dir: Path
    shape: tuple[int, ...]
    rows_flipped: int
    finite_before: int
    nan_before: int
    nan_after: int
    sha256_before: dict[str, str] = field(default_factory=dict)
    sha256_staged: dict[str, str] = field(default_factory=dict)
    sha256_after: dict[str, str] = field(default_factory=dict)
    verified: bool = False


def _stage(
    target: Target,
    live_root: Path,
    stage_root: Path,
    fault: str | None,
) -> StagedArray:
    """Write the corrected array beside the live one, through the live metadata.

    `zarr.json` is copied verbatim, so the staged array carries the live
    array's codecs, chunking, sharding and fill value exactly; nothing here
    creates an array through a layout policy.  Every slice covers a whole
    shard (the last one whatever remains), so no shard is read-modify-written.
    """
    live_dir = live_root / target.group / target.array
    source = zarr.open_array(live_dir, mode="r")
    if source.dtype != np.float32:
        raise FlipRefusal(
            f"{target.label} is stored as {source.dtype}; this correction rewrites the "
            "format's float32 frequency planes only"
        )
    if source.ndim != 1:
        raise FlipRefusal(
            f"{target.label} is {source.ndim}-D; this correction only rewrites the 1-D "
            "variant axis of eaf_reference and top-hit eaf"
        )
    imputed = None
    if target.imputed_filter:
        imputed_dir = live_root / target.group / "imputed"
        if not imputed_dir.is_dir():
            raise FlipRefusal(
                f"{target.label} needs {target.group}/imputed to say which hits are imputed, "
                "and the tier carries none"
            )
        imputed = zarr.open_array(imputed_dir, mode="r")
        if imputed.shape != source.shape:
            raise FlipRefusal(
                f"{target.group}/imputed has shape {imputed.shape} but {target.group}/eaf has "
                f"{source.shape}; the tier disagrees with itself"
            )

    staged_dir = stage_root / target.group / f".{target.array}{STAGING_SUFFIX}"
    if os.path.lexists(staged_dir):
        raise FlipRefusal(
            f"{staged_dir} already exists: a previous run left a staged "
            f"{target.label} behind. Inspect it and remove it before running again."
        )
    staged_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _stage_into(target, source, imputed, live_dir, staged_dir, fault)
    except BaseException:
        # Nothing was swapped, so a staging or verification failure leaves the
        # Store exactly as it was: this run's staged copy goes away with it.
        shutil.rmtree(staged_dir, ignore_errors=True)
        raise


def _stage_into(
    target: Target,
    source: Any,
    imputed: Any,
    live_dir: Path,
    staged_dir: Path,
    fault: str | None,
) -> StagedArray:
    staged_dir.mkdir()
    shutil.copyfile(live_dir / "zarr.json", staged_dir / "zarr.json")
    staged = zarr.open_array(staged_dir, mode="r+")

    n_rows = int(source.shape[0])
    finite_before = 0
    nan_before = 0
    rows_flipped = 0
    for start, stop in _slice_rows(n_rows, _shard_rows(source)):
        old = np.asarray(source[start:stop])
        mask = None
        if imputed is not None:
            mask = np.asarray(imputed[start:stop]).astype(bool)
            if mask.shape != old.shape:
                raise FlipRefusal(
                    f"{target.group}/imputed is not row-aligned with {target.group}/eaf"
                )
            rows_flipped += int((np.isfinite(old) & mask).sum())
        finite_before += int(np.isfinite(old).sum())
        nan_before += int(np.isnan(old).sum())
        staged[start:stop] = _flip(old, mask)
    if imputed is None:
        rows_flipped = finite_before
    staged_meta_before = (staged_dir / "zarr.json").read_bytes()

    if fault == VERIFY_FAULT:
        raise VerificationFailure(
            f"test fault: refusing before verifying the staged {target.label} ({FAIL_AT_ENV})"
        )

    verified, nan_after = _verify_staged(target, source, staged, imputed, nan_before)
    if (staged_dir / "zarr.json").read_bytes() != staged_meta_before:
        raise VerificationFailure(f"verifying the staged {target.label} rewrote its zarr.json")
    _fsync_tree(staged_dir)
    return StagedArray(
        target=target,
        staged_dir=staged_dir,
        live_dir=live_dir,
        shape=tuple(int(n) for n in source.shape),
        rows_flipped=rows_flipped,
        finite_before=finite_before,
        nan_before=nan_before,
        nan_after=nan_after,
        verified=verified,
    )


def _verify_staged(
    target: Target,
    source: Any,
    staged: Any,
    imputed: Any,
    nan_before: int,
) -> tuple[bool, int]:
    """Prove the staged array is exactly the transform of the live one.

    The comparison is on raw float32 bit patterns -- not `isclose`, not
    `equal_nan` -- because the requirement is that `1 - x` was applied and that
    everything else (NaNs and their payloads included) is untouched.  For a
    filtered tier the unflipped rows must be bit-identical, which the same
    comparison covers.
    """
    n_rows = int(source.shape[0])
    nan_after = 0
    for start, stop in _slice_rows(n_rows, _shard_rows(source)):
        old = np.asarray(source[start:stop])
        mask = None
        if imputed is not None:
            mask = np.asarray(imputed[start:stop]).astype(bool)
        expected = _flip(old, mask)
        got = np.asarray(staged[start:stop])
        if got.dtype != expected.dtype:
            raise VerificationFailure(
                f"the staged {target.label} decodes as {got.dtype}, not {expected.dtype}"
            )
        nan_after += int(np.isnan(got).sum())
        if not np.array_equal(got.view(np.uint32), expected.view(np.uint32)):
            raise VerificationFailure(
                f"the staged {target.label} rows [{start}:{stop}) are not 1 - x on the "
                "finite cells of the live array (bit patterns differ)"
            )
    if nan_after != nan_before:
        raise VerificationFailure(
            f"the staged {target.label} holds {nan_after} NaNs; the live array holds {nan_before}"
        )
    return True, nan_after


def _verify_hm3(primary: Any, hm3: Any, subset: Any, *, what: str) -> None:
    """The HM3 copy must equal the primary at its variants, bit for bit.

    `validate` requires exactly this (`_eaf_reference_error`), so a correction
    that flipped both arrays with the wrong row alignment would be caught here
    rather than hours later by `validate`.
    """
    index = np.asarray(subset[:], dtype=np.int64)
    restricted = np.asarray(primary.oindex[index], dtype=np.float32)
    replicate = np.asarray(hm3[:], dtype=np.float32)
    if replicate.shape != restricted.shape:
        raise VerificationFailure(
            f"{what}: indexed_subsets/hm3/eaf_reference has shape {replicate.shape} but "
            f"eaf_reference[variant_index] has {restricted.shape}"
        )
    if not np.array_equal(restricted.view(np.uint32), replicate.view(np.uint32)):
        raise VerificationFailure(
            f"{what}: indexed_subsets/hm3/eaf_reference does not equal the primary "
            "eaf_reference at its variant_index rows"
        )


# ---------------------------------------------------------------------------
# The swap, its progress, and its rollback
# ---------------------------------------------------------------------------


def _rename(source: Path, dest: Path) -> None:
    """One atomic rename.  The single seam a test injects a failure through."""
    os.replace(source, dest)


def _fsync_path(path: Path) -> None:
    """fsync a file or directory, raising: durability that failed must be said.

    Deliberately not `run._fsync_dir`, which swallows every exception: the
    recovery evidence is only worth keeping if a failure to persist it is loud
    enough to stop this run from deleting it.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    """fsync a regular file, raising on failure."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(staged_dir: Path) -> None:
    """Make a staged array durable before it is swapped in.

    Every file, every directory under it, the directory itself and its parent
    are fsynced, and a failure raises: a rename of data the disk has not yet
    got would leave a live array that a crash can truncate with the original
    already moved aside.
    """
    for path in sorted(staged_dir.rglob("*")):
        if path.is_file():
            _fsync_file(path)
    for path in sorted(staged_dir.rglob("*"), reverse=True):
        if path.is_dir():
            _fsync_path(path)
    _fsync_path(staged_dir)
    _fsync_path(staged_dir.parent)


def _fsync_parents(source: Path, dest: Path) -> None:
    """fsync both parents of a rename: the source's and the destination's."""
    _fsync_path(source.parent)
    if dest.parent != source.parent:
        _fsync_path(dest.parent)


def _make_directory_durable(directory: Path) -> None:
    """`mkdir -p`, fsyncing every directory this call creates and its parent."""
    missing: list[Path] = []
    current = directory
    while not os.path.lexists(current):
        missing.append(current)
        if current.parent == current:  # pragma: no cover - a filesystem root
            break
        current = current.parent
    for created in reversed(missing):
        created.mkdir(exist_ok=True)
        _fsync_path(created)
        _fsync_path(created.parent)


def _entry_for(record: dict[str, Any], label: str) -> dict[str, Any]:
    for entry in record["replaced"]["arrays"]:
        if entry["array"] == label:
            return entry
    raise FlipRefusal(f"the receipt has no in-flight entry for {label}")  # pragma: no cover


def _set_state(record: dict[str, Any], record_path: Path, label: str, state: str) -> None:
    """Record one array's progress and fsync it, so a death leaves the truth."""
    _entry_for(record, label)["state"] = state
    _write_receipt(record, record_path)


def _swap_all(
    plan: "Plan | ResyncPlan",
    record: dict[str, Any],
    record_path: Path,
    fault: str | None,
) -> None:
    """Rename each original aside and its staged copy in, announcing each rename.

    Every rename is preceded by an `-ing` state written and fsynced, and
    followed by the completed state, written after both parents of the rename
    are fsynced.  A death anywhere in between therefore leaves a receipt that
    says what was being attempted, and a filesystem that says what happened.
    """
    for staged in plan.staged:
        label = staged.target.label
        entry = _entry_for(record, label)
        original = Path(entry["original"])
        if os.path.lexists(original):
            raise FlipRefusal(f"the rollback destination {original} already exists")
        if fault == SWAP_FAULT:
            raise FlipRefusal(f"test fault: refusing before the swap of {label} ({FAIL_AT_ENV})")
        _make_directory_durable(original.parent)
        _set_state(record, record_path, label, MOVING_ASIDE)
        _rename(staged.live_dir, original)
        _fsync_parents(staged.live_dir, original)
        _set_state(record, record_path, label, MOVED_ASIDE)
        _set_state(record, record_path, label, SWAPPING_IN)
        _rename(staged.staged_dir, staged.live_dir)
        _fsync_parents(staged.staged_dir, staged.live_dir)
        _set_state(record, record_path, label, SWAPPED)


def _restore(
    plan: "Plan | ResyncPlan",
    record: dict[str, Any],
    record_path: Path,
) -> list[str]:
    """Put every original back and prove it; return what could not be proven.

    A failure of any part -- the rename, its parent fsync, the hashing that
    proves the result, or writing the state -- is caught per array, so one
    unprovable array cannot stop the others being restored, and none of them is
    reported as `restored` on a promise.
    """
    failures: list[str] = []
    for staged in reversed(plan.staged):
        label = staged.target.label
        entry = _entry_for(record, label)
        original = Path(entry["original"])
        restore_error: str | None = None
        if os.path.lexists(original):
            try:
                _set_state(record, record_path, label, RESTORING)
                if os.path.lexists(staged.live_dir):
                    # The corrected copy goes back to its staging name first,
                    # so the live name is free for the original.
                    _rename(staged.live_dir, staged.staged_dir)
                    _fsync_parents(staged.live_dir, staged.staged_dir)
                _rename(original, staged.live_dir)
                _fsync_parents(original, staged.live_dir)
            except OSError as exc:
                restore_error = f"{label}: cannot restore it ({exc})"
                failures.append(restore_error)
        matches = False
        try:
            matches = bool(staged.sha256_before) and _sha256_tree(staged.live_dir) == staged.sha256_before
            if not matches and restore_error is None:
                failures.append(f"{label}: the live array does not hash to its recorded before-hashes")
        except OSError as exc:
            failures.append(f"{label}: cannot hash the live array to prove it restored ({exc})")
        # `restored` means every step proved: the rename, its durability, and
        # the bytes.  A rename whose parent fsync failed restored nothing that
        # can be relied on if the machine dies, so it is not called restored.
        restored = matches and restore_error is None
        try:
            _set_state(record, record_path, label, RESTORED if restored else FAILED_RESTORATION)
        except OSError as exc:
            failures.append(f"{label}: cannot record its state ({exc})")
    return failures


# ---------------------------------------------------------------------------
# Describing a Store from the filesystem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrayState:
    """What one target array looks like on disk, and how it hashes."""

    label: str
    state: str | None
    live_path: Path
    staged_path: Path
    original_path: Path | None
    live_kind: str  # original | corrected | absent | unrecognised | unreadable
    live_matches_before: bool | None
    original_present: bool
    original_matches_before: bool | None
    staged_present: bool
    error: str | None = None

    def describe(self) -> str:
        """One line an operator can act on: live first, then the evidence beside it."""
        if self.live_kind == "absent":
            text = "live missing"
            if self.original_present:
                text += f", original at {self.original_path}"
                if self.original_matches_before is True:
                    text += " (hashes to its recorded before-hashes)"
                elif self.original_matches_before is False:
                    text += " (does NOT hash to its recorded before-hashes)"
            else:
                text += ", no original in replaced/"
        elif self.live_kind == "original":
            text = "original live"
        elif self.live_kind == "corrected":
            text = "corrected live"
        elif self.live_kind == "present":
            text = "live present (the receipt records no hashes to compare it with)"
        elif self.live_kind == "unreadable":
            text = f"live present but unreadable ({self.error})"
        else:
            text = "live present but matches neither recorded state"
        if self.live_kind != "absent" and self.original_present:
            text += f", original at {self.original_path}"
        if self.staged_present:
            text += f", corrected copy staged at {self.staged_path}"
        if self.state:
            # Said last and labelled: a kill leaves the receipt one transition
            # behind, so it is reported as what the run claimed, not as fact.
            text += f" (receipt says: {self.state})"
        return text


def _recorded_arrays(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        entry["array"]: entry
        for entry in record.get("arrays") or []
        if isinstance(entry, dict) and isinstance(entry.get("array"), str)
    }


def _recorded_in_flight(record: dict[str, Any]) -> list[dict[str, Any]]:
    replaced = record.get("replaced") or {}
    return [entry for entry in replaced.get("arrays") or [] if isinstance(entry, dict)]


def _replaced_dirs(store_path: Path, suffix: str = REPLACED_SUFFIX) -> list[Path]:
    """This Store's `replaced/<UTC>-<suffix>/` directories, if any."""
    return sorted((store_path.parent / "replaced").glob(f"*-{suffix}"))


def _inspect_existing(
    store_path: Path,
    record: dict[str, Any],
    suffix: str = REPLACED_SUFFIX,
    staging: str = STAGING_SUFFIX,
) -> list[ArrayState]:
    """What the filesystem says about every array a receipt names.

    The receipt can be one transition behind a kill (it is written before and
    after each rename, not atomically with it), so nothing here trusts its
    `state`: the live path, the staged sibling and the original under
    `replaced/` are looked for, and each is hashed against the before- and
    corrected-hashes the receipt recorded.
    """
    hashes = _recorded_arrays(record)
    in_flight = _recorded_in_flight(record)
    labels = [
        entry["array"] for entry in in_flight if isinstance(entry.get("array"), str)
    ] or list(hashes)
    replaced_dirs = _replaced_dirs(store_path, suffix)
    recorded_dir = (record.get("replaced") or {}).get("dir")
    states: list[ArrayState] = []
    for label in labels:
        entry = next((e for e in in_flight if e.get("array") == label), {})
        live = Path(entry["live"]) if entry.get("live") else store_path / "data.zarr" / label
        staged = (
            Path(entry["staged"])
            if entry.get("staged")
            else live.parent / f".{live.name}{staging}"
        )
        if entry.get("original"):
            original: Path | None = Path(entry["original"])
        elif recorded_dir:
            original = Path(recorded_dir) / label
        elif len(replaced_dirs) == 1:
            original = replaced_dirs[0] / label
        else:
            original = None
        before = hashes.get(label, {}).get("sha256_before") or {}
        corrected = (
            hashes.get(label, {}).get("sha256_corrected")
            or hashes.get(label, {}).get("sha256_after")
            or {}
        )
        error: str | None = None
        live_tree: dict[str, str] = {}
        if live.exists():
            try:
                live_tree = _sha256_tree(live)
            except OSError as exc:
                error = str(exc)
        original_present = original is not None and os.path.lexists(original)
        original_before: bool | None = None
        if original_present:
            try:
                original_before = bool(before) and _sha256_tree(original) == before
            except OSError:
                original_before = None
        if error is not None:
            kind = "unreadable"
            matches_before: bool | None = None
        elif not live_tree:
            kind, matches_before = "absent", None
        elif before and live_tree == before:
            kind, matches_before = "original", True
        elif corrected and live_tree == corrected:
            kind, matches_before = "corrected", False
        elif not before and not corrected:
            # A receipt written by hand (or by a version that recorded none):
            # presence is all that can be said.
            kind, matches_before = "present", None
        else:
            kind, matches_before = "unrecognised", (False if before else None)
        states.append(
            ArrayState(
                label=label,
                state=entry.get("state") if isinstance(entry.get("state"), str) else None,
                live_path=live,
                staged_path=staged,
                original_path=original,
                live_kind=kind,
                live_matches_before=matches_before,
                original_present=original_present,
                original_matches_before=original_before,
                staged_present=os.path.lexists(staged),
                error=error,
            )
        )
    return states


def _describe_store(states: list[ArrayState]) -> list[str]:
    if not states:
        return ["  (the receipt names no array)"]
    return [f"  {state.label}: {state.describe()}" for state in states]


def _failed_restoration_message(
    plan: "Plan | ResyncPlan", record: dict[str, Any], cause: BaseException, failures: list[str]
) -> str:
    """Describe the real live state of every array, then what could not be proven."""
    record_path = paths.record_path(plan.store_id, plan.step, root=plan.artifact_root)
    try:
        states = _inspect_existing(plan.store_path, record, plan.replaced_suffix, plan.staging_suffix)
    except OSError:  # pragma: no cover - hashing failures are caught per array above
        states = []
    lines = [
        f"the swap of {plan.store_id} failed: {cause}",
        "Recovery could not prove every array restored, so nothing was deleted. "
        f"The receipt is {record_path}; the Store, read from the filesystem:",
    ]
    lines += _describe_store(states)
    lines += [f"  {failure}" for failure in failures]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The plan and the receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """Everything the correction will do, measured before it does anything."""

    store_id: str
    artifact_root: Path
    store_path: Path
    staged: list[StagedArray]
    before: Guard
    after: Guard
    revision: str
    version: str
    replaced_dir: Path
    step: str = RECORD_STEP
    replaced_suffix: str = REPLACED_SUFFIX
    staging_suffix: str = STAGING_SUFFIX


def _entry(staged: StagedArray) -> dict[str, Any]:
    return {
        "array": staged.target.label,
        "group": staged.target.group,
        "name": staged.target.array,
        "imputed_filter": staged.target.imputed_filter,
        "shape": list(staged.shape),
        "finite_before": staged.finite_before,
        "rows_flipped": staged.rows_flipped,
        "nan_before": staged.nan_before,
        "nan_after": staged.nan_after,
        "sha256_before": staged.sha256_before,
        "sha256_corrected": staged.sha256_staged,
        "sha256_after": staged.sha256_after,
        "verified": "new == 1 - old on finite cells (raw float32 bits); "
        "NaN positions and payloads and unflipped rows bit-identical",
    }


def _in_flight(plan: Plan) -> list[dict[str, Any]]:
    """The arrays this run may touch, each `pending` before its first rename."""
    return [
        {
            "array": staged.target.label,
            "state": PENDING,
            "original": str(plan.replaced_dir / staged.target.group / staged.target.array),
            "live": str(staged.live_dir),
            "staged": str(staged.staged_dir),
        }
        for staged in plan.staged
    ]


def _started_record(plan: Plan, started_at: str) -> dict[str, Any]:
    """The receipt written immediately before the first rename.

    It carries every hash and count the run has proved so far, so a process
    killed between the two renames leaves an account of what to restore and
    which file is which.
    """
    return {
        "status": "started",
        "step": RECORD_STEP,
        "issue": 409,
        "store_id": plan.store_id,
        "store_path": str(plan.store_path),
        "artifact_root": str(plan.artifact_root),
        "started_at": started_at,
        "completed_at": None,
        "failed_at": None,
        "opengwasdb_revision": plan.revision,
        "opengwasdb_version": plan.version,
        "tool": {"path": TOOL_PATH, "commit": _tool_commit()},
        "guard": {
            "sample_seed": plan.before.seed,
            "band_rows": plan.before.band_rows,
            "bands": plan.before.bands,
            "sampled_variants": plan.before.sampled,
            "before": {"r": plan.before.r, "n": plan.before.n},
            "after": {"r": plan.after.r, "n": plan.after.n},
        },
        "arrays": [_entry(staged) for staged in plan.staged],
        "replaced": {"dir": str(plan.replaced_dir), "arrays": _in_flight(plan)},
    }


def _describe(plan: Plan, *, dry_run: bool) -> str:
    lines = [
        f"eaf-reference-flip for {plan.store_id}",
        f"  store:         {plan.store_path}",
        f"  core revision: {plan.revision} ({plan.version})",
        f"  orientation:   {plan.before.describe()}",
        f"  after staging: {plan.after.describe()}",
        f"  {'would rewrite' if dry_run else 'rewrote'} {len(plan.staged)} array(s):",
    ]
    for staged in plan.staged:
        filter_note = " (imputed rows only)" if staged.target.imputed_filter else ""
        lines.append(
            f"    {staged.target.label}: shape {list(staged.shape)}, "
            f"{staged.rows_flipped} of {staged.finite_before} finite row(s) flipped{filter_note}, "
            f"{staged.nan_before} NaN row(s) kept"
        )
    lines.append(f"  originals {'would move' if dry_run else 'moved'} to {plan.replaced_dir}")
    return "\n".join(lines)


def _build_plan(
    store_id: str,
    artifact_root: Path,
    store_path: Path,
    stage_root: Path,
    seed: int,
    revision: str,
    version: str,
) -> Plan:
    """Steps 2-5: scope, guard, stage, verify, guard again -- writing only `stage_root`."""
    data_zarr = store_path / "data.zarr"
    manifest = _read_manifest(store_path)
    _refuse_out_of_scope(store_path, manifest)
    try:
        encoding = StoreEncoding.from_manifest(manifest["encoding"])
    except (KeyError, ValueError, TypeError) as exc:
        raise FlipRefusal(
            f"{store_id} declares no encoding plan this runtime can read ({exc}); the guard "
            "cannot decode its observed EAF"
        ) from exc
    targets = _targets(data_zarr)
    root = zarr.open_group(data_zarr, mode="r")
    if "imputed" not in root:
        raise FlipRefusal(
            f"{data_zarr} carries no `imputed` completion mask; a reference-completed "
            "Store must have one, and without it the guard cannot tell an observed cell "
            "from a panel-derived one"
        )
    sample = _sample_observed(root, encoding, seed)
    before = _measure(sample, root["eaf_reference"])
    _require_inverted(before)

    fault = _fault_at()
    staged: list[StagedArray] = []
    try:
        for target in targets:
            staged.append(_stage(target, data_zarr, stage_root, fault))

        hm3 = next((s for s in staged if s.target.group == "indexed_subsets/hm3"), None)
        if hm3 is not None:
            primary_staged = next(s for s in staged if s.target.group == "")
            subset = root["indexed_subsets/hm3/variant_index"]
            _verify_hm3(
                zarr.open_array(primary_staged.staged_dir, mode="r"),
                zarr.open_array(hm3.staged_dir, mode="r"),
                subset,
                what="staged",
            )
            _verify_hm3(root["eaf_reference"], root["indexed_subsets/hm3/eaf_reference"], subset, what="live")

        primary_staged = zarr.open_array(staged[0].staged_dir, mode="r")
        after = _measure(sample, primary_staged)
        _require_corrected(after)
    except BaseException:
        # A failed run leaves no staged array behind: the Store is untouched,
        # so there is nothing to recover from and nothing to inspect.
        for entry in staged:
            shutil.rmtree(entry.staged_dir, ignore_errors=True)
        raise

    stamp = f"{run.utc_stamp()}-{REPLACED_SUFFIX}"
    return Plan(
        store_id=store_id,
        artifact_root=artifact_root,
        store_path=store_path,
        staged=staged,
        before=before,
        after=after,
        revision=revision,
        version=version,
        replaced_dir=paths.replaced_dir(store_id, stamp, root=artifact_root),
    )


def _hash_live(plan: Plan) -> None:
    """Record each touched array's per-file sha256, before the swap."""
    for staged in plan.staged:
        staged.sha256_before = _sha256_tree(staged.live_dir)
        staged.sha256_staged = _sha256_tree(staged.staged_dir)


def _refuse_existing_receipt(
    store_id: str,
    artifact_root: Path,
    store_path: Path,
    *,
    step: str = RECORD_STEP,
    suffix: str = REPLACED_SUFFIX,
    staging: str = STAGING_SUFFIX,
    consequence: str = (
        "This tool refuses: a second flip would restore the inversion the first one corrects."
    ),
) -> None:
    """A receipt of any status refuses, and its message describes the real Store.

    `1 - (1 - x)` is `x`, so a second flip is always refused.  What the operator
    is told is read from the filesystem rather than from the receipt's own
    `state`, which a kill can leave one transition behind: each live array is
    hashed against the before- and corrected-hashes, and each original in
    `replaced/` against its before-hashes.  "No array moved" is claimed only
    when that inspection proves it, and only then is clearing the receipt
    suggested.
    """
    record_path = paths.record_path(store_id, step, root=artifact_root)
    if not os.path.lexists(record_path):
        return
    receipt: dict[str, Any] = {}
    if record_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            loaded = json.loads(record_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                receipt = loaded
    status = receipt.get("status", "unreadable")
    try:
        states = _inspect_existing(store_path, receipt, suffix, staging)
    except OSError as exc:  # pragma: no cover - hashing failures are caught per array
        states = []
        inspection_error = str(exc)
    else:
        inspection_error = None
    replaced_present = bool(_replaced_dirs(store_path, suffix))
    untouched = (
        bool(states)
        and not replaced_present
        and all(state.live_kind in ("original", "unrecognised", "present") for state in states)
        and all(state.live_matches_before is not False for state in states)
    )
    lines = [
        f"{record_path} already exists (status {status!r}).",
        consequence,
        "The Store, read from the filesystem:",
    ]
    lines += _describe_store(states)
    if inspection_error is not None:
        lines.append(f"  the Store could not be inspected: {inspection_error}")
    staged = [state.staged_path for state in states if state.staged_present]
    if untouched:
        lines.append(
            "No array moved: every live array is in place and no original sits in "
            "`replaced/`, so the Store is as it was before this run. Clear the receipt "
            f"deliberately (`rm {record_path}`)"
            + (
                ", remove this run's staged copies ("
                + ", ".join(str(path) for path in staged)
                + ")"
                if staged
                else ""
            )
            + " once you have checked the Store, then run again."
        )
    else:
        lines.append(
            "Do not clear the receipt: at least one array is not in the state this run "
            "started from. Restore each array from the original named above (or the "
            "`replaced/` directory), verify the Store, and only then remove the receipt "
            "and re-run."
        )
    raise FlipRefusal("\n".join(lines))


def _write_receipt(record: dict[str, Any], record_path: Path) -> None:
    """Atomically rewrite the receipt, fsyncing the file and then its directory.

    `run._write_record_atomically` is not used: it swallows a failure to fsync the
    directory, and a receipt that may not have reached the disk is exactly the
    evidence this tool must not lose without saying so.
    """
    record_path.parent.mkdir(parents=True, exist_ok=True)
    temp = record_path.with_name(f".{record_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(run.record_text(record))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, record_path)
    _fsync_path(record_path.parent)


def _apply(plan: Plan) -> int:
    """Swap the verified arrays in, then finish the receipt.  Steps 5-6."""
    record_path = paths.record_path(plan.store_id, RECORD_STEP, root=plan.artifact_root)
    _hash_live(plan)
    record = _started_record(plan, _precedent._utc_now())
    _write_receipt(record, record_path)

    try:
        _swap_all(plan, record, record_path, _fault_at())
    except BaseException as exc:
        failures = _restore(plan, record, record_path)
        if failures:
            record["status"] = FAILED_RESTORATION
            record["failed_at"] = _precedent._utc_now()
            try:
                _write_receipt(record, record_path)
            except OSError as write_exc:
                failures.append(f"the receipt could not be finalised ({write_exc})")
            raise FlipRefusal(
                _failed_restoration_message(plan, record, exc, failures)
            ) from exc
        # Every array is proven restored: this run's staged copies and the
        # receipt go, and the rollback directory with them.
        for staged in plan.staged:
            shutil.rmtree(staged.staged_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            record_path.unlink()
        shutil.rmtree(plan.replaced_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            plan.replaced_dir.parent.rmdir()  # only when this run made it and it is empty
        raise FlipRefusal(
            f"the swap of {plan.store_id} failed: {exc}. Every array is restored (proven by "
            "the recorded before-hashes), and this run's staged copies and receipt were removed."
        ) from exc

    for staged in plan.staged:
        staged.sha256_after = _sha256_tree(staged.live_dir)
        if staged.sha256_after != staged.sha256_staged:
            raise FlipRefusal(
                f"{staged.target.label} was swapped in but its files do not match the "
                f"verified staged copy (see {record_path}); restore it from {plan.replaced_dir}"
            )

    record["status"] = "complete"
    record["completed_at"] = _precedent._utc_now()
    record["arrays"] = [_entry(staged) for staged in plan.staged]
    _write_receipt(record, record_path)

    print(_describe(plan, dry_run=False))
    for staged in plan.staged:
        print(f"  {staged.target.label}: {staged.rows_flipped} row(s) flipped, swapped in")
    print(f"eaf-reference-flip: {plan.store_id} now reads {plan.after.describe()}")
    print(f"eaf-reference-flip: originals kept in {plan.replaced_dir}")
    print(f"eaf-reference-flip: receipt {record_path}")
    return 0


# ---------------------------------------------------------------------------
# The follow-on: re-sync the top-hit SE to the decoded plane
# ---------------------------------------------------------------------------
#
# A residual SE plane is decoded against the frequency of the same cell, and an
# imputed cell's frequency is `eaf_reference`.  The flip replaces that value
# with `float32(1) - f`, which is not an exact inverse of the earlier
# `float32(1) - f`: near 1 float32 resolves about 6e-8 in absolute terms, so a
# rare variant's `1 - (1 - f)` differs from `f` in its last bits.  The
# prediction `c0 + c1 * log(2 f (1 - f))` moves with it, and the decoded SE of an
# imputed cell changes by up to about `|c1| * 3e-8 / f` relative -- far below the
# codec's half step, but above `validate`'s `rtol=1e-6, atol=0` against the SE a
# top-hit tier copied from the plane before the flip.  The top-hit SE is a
# derived copy written from the decoded plane, so it is written again.

#: The receipt and `replaced/` stamp of the follow-on.
RESYNC_STEP: str = "eaf-reference-flip-se-resync"
RESYNC_REPLACED_SUFFIX: str = "eaf-reference-flip-se-resync"

#: The suffix a staged top-hit `se` carries inside its own tier group.
RESYNC_STAGING_SUFFIX: str = ".resyncing"

#: No decoded SE may move by more than this, relative, or the cause is not the
#: float32 round trip this step exists for and nothing is written.  The largest
#: possible move is about `|c1| * 3e-8 / f` at the panel's 1% frequency floor.
MAX_RESYNC_REL: float = 1e-5

#: `validate`'s comparison of a tier's SE with the decoded plane.
VALIDATE_SE_RTOL: float = 1e-6

#: Worker processes of the core's top-hit SE collector unless `--workers` says
#: otherwise.  Each runs with Zarr async width and Blosc threads of one, so the
#: concurrent decodes are `--workers` and never `--workers` times a width: the
#: #326 cap (`se_reencode.MAX_WORKERS`) holds.
DEFAULT_DECODE_WORKERS: int = _precedent.DEFAULT_WORKERS

SE_DECODE_FAULT = "se-decode"
RESYNC_VERIFY_FAULT = "se-resync-verify"


def _se_is_residual(manifest: dict[str, Any]) -> bool:
    """Whether the Store's SE plane is decoded against EAF (and so against the reference)."""
    try:
        return bool(StoreEncoding.from_manifest(manifest["encoding"]).se.is_residual)
    except (KeyError, ValueError, TypeError) as exc:
        raise FlipRefusal(
            f"the Store declares no encoding plan this runtime can read ({exc}); whether "
            "its SE depends on eaf_reference cannot be decided"
        ) from exc


def _decode_top_hit_se(
    store_path: Path,
    encoding: StoreEncoding,
    rows: np.ndarray,
    cols: np.ndarray,
    workers: int,
) -> np.ndarray:
    """The SE `(rows[i], cols[i])` decodes to, exactly as the core writes a top-hit tier.

    The core's top-hit writer takes each tier's `se` from `DenseSePlane.band`
    gathered row chunk by row chunk and stored as float32.  That is the function
    called here rather than a copy of it, so the value written back is the
    value a rebuild would write.
    """
    try:
        from opengwasdb.layouts.dense import top_hits as _core_top_hits

        collect = _core_top_hits._collect_top_hit_se
    except (ImportError, AttributeError) as exc:
        raise FlipRefusal(
            "the pinned runtime does not offer the top-hit writer's SE collector "
            f"(opengwasdb.layouts.dense.top_hits._collect_top_hit_se): {exc}"
        ) from exc
    # The collector forks `workers` processes; each decodes with one thread at
    # every level, so processes x width stays within the precedent's bound.
    _precedent._apply_worker_config(1)
    return np.asarray(collect(store_path, rows, cols, encoding, workers), dtype=np.float32)


@dataclass
class TierSync:
    """One top-hit tier's stored SE beside the SE its decoded plane gives now."""

    tier: str
    cells: int
    imputed_cells: int
    stored: np.ndarray
    decoded: np.ndarray
    changed: np.ndarray
    validate_mismatches: int
    max_relative_change: float

    @property
    def rows_changed(self) -> int:
        return int(self.changed.sum())


def _bits(values: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(values, dtype=np.float32).view(np.uint32)


def _compare_tier(tier: str, stored: np.ndarray, decoded: np.ndarray, imputed: np.ndarray) -> TierSync:
    """Which cells moved, with the guards that say the movement is the expected kind."""
    if stored.shape != decoded.shape or stored.shape != imputed.shape:
        raise FlipRefusal(
            f"top_hits/{tier}: se has {stored.shape[0]} cells, its imputed mask "
            f"{imputed.shape[0]}, and the decoded plane returned {decoded.shape[0]}"
        )
    changed = _bits(stored) != _bits(decoded)
    movers = int(changed.sum())
    outside = changed & ~imputed
    if outside.any():
        raise FlipRefusal(
            f"top_hits/{tier}: {int(outside.sum())} observed (imputed == 0) cell(s) would change "
            "their SE; only an imputed cell's SE depends on eaf_reference, so this is a "
            "different defect and nothing was written"
        )
    max_rel = 0.0
    if movers:
        old = stored[changed].astype(np.float64)
        new = decoded[changed].astype(np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            rel = np.abs(new - old) / np.abs(old)
        if not np.all(np.isfinite(rel)) or float(rel.max()) > MAX_RESYNC_REL:
            worst = "non-finite" if not np.all(np.isfinite(rel)) else f"{float(rel.max()):.3g}"
            raise FlipRefusal(
                f"top_hits/{tier}: a decoded SE moves by {worst} relative (limit "
                f"{MAX_RESYNC_REL:g}); a reference-frequency round trip cannot do that, so "
                "nothing was written"
            )
        max_rel = float(rel.max())
    mismatched = ~np.isclose(decoded, stored, rtol=VALIDATE_SE_RTOL, atol=0.0, equal_nan=True)
    return TierSync(
        tier=tier,
        cells=int(stored.shape[0]),
        imputed_cells=int(imputed.sum()),
        stored=stored,
        decoded=decoded,
        changed=changed,
        validate_mismatches=int(mismatched.sum()),
        max_relative_change=max_rel,
    )


def _require_complete_flip(store_id: str, artifact_root: Path, store_path: Path) -> dict[str, Any]:
    """The flip's receipt, proven complete and matched by the live arrays.

    The follow-on re-reads decoded values, so it must run against the corrected
    Store and no other: an incomplete or contradicted flip receipt means the
    Store is in a state this step cannot reason about.
    """
    record_path = paths.record_path(store_id, RECORD_STEP, root=artifact_root)
    if not record_path.is_file():
        raise FlipRefusal(
            f"{record_path} does not exist: the SE re-sync follows a completed flip, and "
            f"{store_id} has not been flipped by this tool"
        )
    try:
        receipt = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FlipRefusal(f"cannot read the flip receipt {record_path}: {exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("status") != "complete":
        status = receipt.get("status") if isinstance(receipt, dict) else "unreadable"
        raise FlipRefusal(
            f"the flip receipt {record_path} has status {status!r}, not 'complete'; finish "
            "or restore the flip before re-syncing anything that depends on it"
        )
    # The live array is where this Store is, not where the receipt remembers it
    # (a Store directory can be moved or restored elsewhere after its flip).
    for entry in _recorded_in_flight(receipt):
        if isinstance(entry.get("array"), str):
            entry["live"] = str(store_path / "data.zarr" / entry["array"])
    states = _inspect_existing(store_path, receipt)
    wrong = [state for state in states if state.live_kind != "corrected"]
    if not states or wrong:
        lines = [
            f"the live arrays do not all match the flip receipt {record_path}; read from the "
            "filesystem:"
        ]
        lines += _describe_store(states)
        raise FlipRefusal("\n".join(lines))
    return receipt


def _resync_targets(root: Any) -> list[str]:
    """The top-hit tiers to re-sync: every tier, each needing what the comparison reads."""
    if "top_hits" not in root:
        return []
    tiers = sorted(root["top_hits"].group_keys())
    for tier in tiers:
        group = root["top_hits"][tier]
        missing = [name for name in ("se", "imputed", "variant_index", "analysis_index") if name not in group]
        if missing:
            raise FlipRefusal(
                f"data.zarr/top_hits/{tier} carries no {' or '.join(missing)}; a tier whose SE "
                "cannot be compared with the decoded plane is refused, not skipped"
            )
    return tiers


@dataclass(frozen=True)
class ResyncPlan:
    """Every change the follow-on will make, measured before it makes any."""

    store_id: str
    artifact_root: Path
    store_path: Path
    staged: list[StagedArray]
    tiers: list[TierSync]
    revision: str
    version: str
    replaced_dir: Path
    flip_receipt: dict[str, Any]
    hm3_before: str | None
    step: str = RESYNC_STEP
    replaced_suffix: str = RESYNC_REPLACED_SUFFIX
    staging_suffix: str = RESYNC_STAGING_SUFFIX


def _hm3_digest(data_zarr: Path) -> str | None:
    """One digest over every file of the HM3 subset, or None when the Store has none."""
    hm3 = data_zarr / "indexed_subsets" / "hm3"
    if not hm3.is_dir():
        return None
    listing = _sha256_tree(hm3)
    return hashlib.sha256(json.dumps(sorted(listing.items())).encode("utf-8")).hexdigest()


def _stage_se(tier: TierSync, live_root: Path, stage_root: Path) -> StagedArray:
    """Write a tier's decoded SE beside the live one, through the live metadata."""
    target = Target(group=f"top_hits/{tier.tier}", array="se", imputed_filter=True)
    live_dir = live_root / target.group / target.array
    source = zarr.open_array(live_dir, mode="r")
    if source.dtype != np.float32 or source.ndim != 1:
        raise FlipRefusal(
            f"{target.label} is stored as {source.ndim}-D {source.dtype}; the top-hit writer "
            "stores a 1-D float32 and this step rewrites nothing else"
        )
    staged_dir = stage_root / target.group / f".{target.array}{RESYNC_STAGING_SUFFIX}"
    if os.path.lexists(staged_dir):
        raise FlipRefusal(
            f"{staged_dir} already exists: a previous run left a staged {target.label} behind. "
            "Inspect it and remove it before running again."
        )
    staged_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        staged_dir.mkdir()
        shutil.copyfile(live_dir / "zarr.json", staged_dir / "zarr.json")
        staged = zarr.open_array(staged_dir, mode="r+")
        metadata = (staged_dir / "zarr.json").read_bytes()
        for start, stop in _slice_rows(int(source.shape[0]), _shard_rows(source)):
            staged[start:stop] = tier.decoded[start:stop]
        if os.environ.get(FAIL_AT_ENV) == RESYNC_VERIFY_FAULT:
            raise VerificationFailure(
                f"test fault: refusing before verifying the staged {target.label} ({FAIL_AT_ENV})"
            )
        got = np.asarray(zarr.open_array(staged_dir, mode="r")[:], dtype=np.float32)
        if not np.array_equal(_bits(got), _bits(tier.decoded)):
            raise VerificationFailure(
                f"the staged {target.label} does not read back as the decoded plane (bit patterns differ)"
            )
        untouched = ~tier.changed
        if not np.array_equal(_bits(got)[untouched], _bits(tier.stored)[untouched]):
            raise VerificationFailure(f"the staged {target.label} changed a row that was not to change")
        if (staged_dir / "zarr.json").read_bytes() != metadata:
            raise VerificationFailure(f"verifying the staged {target.label} rewrote its zarr.json")
        _fsync_tree(staged_dir)
    except BaseException:
        shutil.rmtree(staged_dir, ignore_errors=True)
        raise
    return StagedArray(
        target=target,
        staged_dir=staged_dir,
        live_dir=live_dir,
        shape=tuple(int(n) for n in source.shape),
        rows_flipped=tier.rows_changed,
        finite_before=int(np.isfinite(tier.stored).sum()),
        nan_before=int(np.isnan(tier.stored).sum()),
        nan_after=int(np.isnan(got).sum()),
        verified=True,
    )


def _build_resync_plan(
    store_id: str,
    artifact_root: Path,
    store_path: Path,
    stage_root: Path,
    revision: str,
    version: str,
    workers: int,
) -> ResyncPlan | None:
    """Scope, flip receipt, decode, guards, stage, verify -- writing only `stage_root`.

    Returns None when the Store's SE does not depend on `eaf_reference` or no
    top-hit SE needs to move: there is nothing to write and no receipt to leave.
    """
    data_zarr = store_path / "data.zarr"
    manifest = _read_manifest(store_path)
    _refuse_out_of_scope(store_path, manifest)
    if not _se_is_residual(manifest):
        return None
    flip_receipt = _require_complete_flip(store_id, artifact_root, store_path)
    encoding = StoreEncoding.from_manifest(manifest["encoding"])
    root = zarr.open_group(data_zarr, mode="r")
    tier_names = _resync_targets(root)

    if os.environ.get(FAIL_AT_ENV) == SE_DECODE_FAULT:
        raise FlipRefusal(f"test fault: refusing before decoding the top-hit SE ({FAIL_AT_ENV})")
    tiers: list[TierSync] = []
    for name in tier_names:
        group = root["top_hits"][name]
        rows = np.asarray(group["variant_index"][:], dtype=np.int64)
        cols = np.asarray(group["analysis_index"][:], dtype=np.int64)
        stored = np.asarray(group["se"][:], dtype=np.float32)
        imputed = np.asarray(group["imputed"][:]).astype(bool)
        if not (len(rows) == len(cols) == len(stored) == len(imputed)):
            raise FlipRefusal(f"top_hits/{name}: its arrays do not have one length")
        decoded = _decode_top_hit_se(store_path, encoding, rows, cols, workers)
        tiers.append(_compare_tier(name, stored, decoded, imputed))

    staged: list[StagedArray] = []
    try:
        for tier in tiers:
            if tier.rows_changed:
                staged.append(_stage_se(tier, data_zarr, stage_root))
    except BaseException:
        for entry in staged:
            shutil.rmtree(entry.staged_dir, ignore_errors=True)
        raise
    if not staged:
        return None
    stamp = f"{run.utc_stamp()}-{RESYNC_REPLACED_SUFFIX}"
    return ResyncPlan(
        store_id=store_id,
        artifact_root=artifact_root,
        store_path=store_path,
        staged=staged,
        tiers=tiers,
        revision=revision,
        version=version,
        replaced_dir=paths.replaced_dir(store_id, stamp, root=artifact_root),
        flip_receipt={
            "path": str(paths.record_path(store_id, RECORD_STEP, root=artifact_root)),
            "sha256": _sha256_file(paths.record_path(store_id, RECORD_STEP, root=artifact_root)),
            "status": flip_receipt.get("status"),
            "completed_at": flip_receipt.get("completed_at"),
        },
        hm3_before=_hm3_digest(data_zarr),
    )


def _resync_entry(staged: StagedArray, tier: TierSync) -> dict[str, Any]:
    return {
        "array": staged.target.label,
        "group": staged.target.group,
        "name": staged.target.array,
        "shape": list(staged.shape),
        "cells": tier.cells,
        "imputed_cells": tier.imputed_cells,
        "rows_changed": tier.rows_changed,
        "max_relative_change": tier.max_relative_change,
        "validate_se_mismatches_before": tier.validate_mismatches,
        "nan_before": staged.nan_before,
        "nan_after": staged.nan_after,
        "sha256_before": staged.sha256_before,
        "sha256_corrected": staged.sha256_staged,
        "sha256_after": staged.sha256_after,
        "verified": "staged se reads back bit-identical to the decoded plane; unchanged rows "
        "bit-identical to the stored se; only imputed rows changed",
    }


def _resync_started_record(plan: ResyncPlan, started_at: str) -> dict[str, Any]:
    by_label = {f"top_hits/{tier.tier}/se": tier for tier in plan.tiers}
    return {
        "status": "started",
        "step": RESYNC_STEP,
        "issue": 409,
        "store_id": plan.store_id,
        "store_path": str(plan.store_path),
        "artifact_root": str(plan.artifact_root),
        "started_at": started_at,
        "completed_at": None,
        "failed_at": None,
        "opengwasdb_revision": plan.revision,
        "opengwasdb_version": plan.version,
        "tool": {"path": TOOL_PATH, "commit": _tool_commit()},
        "flip_receipt": plan.flip_receipt,
        "check": {
            "rtol": 0.0,
            "max_allowed_relative_change": MAX_RESYNC_REL,
            "validate_rtol": VALIDATE_SE_RTOL,
            "tiers": [
                {
                    "tier": tier.tier,
                    "cells": tier.cells,
                    "imputed_cells": tier.imputed_cells,
                    "rows_changed": tier.rows_changed,
                    "max_relative_change": tier.max_relative_change,
                    "validate_se_mismatches_before": tier.validate_mismatches,
                }
                for tier in plan.tiers
            ],
        },
        "hm3_sha256_before": plan.hm3_before,
        "hm3_sha256_after": None,
        "arrays": [_resync_entry(staged, by_label[staged.target.label]) for staged in plan.staged],
        "replaced": {"dir": str(plan.replaced_dir), "arrays": _in_flight(plan)},  # type: ignore[arg-type]
    }


def _describe_resync(plan: ResyncPlan, *, dry_run: bool) -> str:
    lines = [
        f"eaf-reference-flip SE re-sync for {plan.store_id}",
        f"  store:         {plan.store_path}",
        f"  core revision: {plan.revision} ({plan.version})",
        f"  flip receipt:  {plan.flip_receipt['path']} ({plan.flip_receipt['status']})",
    ]
    for tier in plan.tiers:
        lines.append(
            f"  top_hits/{tier.tier}/se: {tier.cells} cell(s), {tier.imputed_cells} imputed; "
            f"{tier.rows_changed} to re-sync (max relative change {tier.max_relative_change:.3g}), "
            f"{tier.validate_mismatches} fail validate's rtol {VALIDATE_SE_RTOL:g} now"
        )
    lines.append(
        f"  {'would rewrite' if dry_run else 'rewrote'} {len(plan.staged)} array(s); originals "
        f"{'would move' if dry_run else 'moved'} to {plan.replaced_dir}"
    )
    return "\n".join(lines)


def _verify_live_se(plan: ResyncPlan, encoding: StoreEncoding, workers: int) -> None:
    """After the swap: every top-hit SE equals a fresh decode of the plane, bit for bit."""
    root = zarr.open_group(plan.store_path / "data.zarr", mode="r")
    for name in _resync_targets(root):
        group = root["top_hits"][name]
        decoded = _decode_top_hit_se(
            plan.store_path,
            encoding,
            np.asarray(group["variant_index"][:], dtype=np.int64),
            np.asarray(group["analysis_index"][:], dtype=np.int64),
            workers,
        )
        live = np.asarray(group["se"][:], dtype=np.float32)
        if not np.array_equal(_bits(live), _bits(decoded)):
            bad = int((_bits(live) != _bits(decoded)).sum())
            raise VerificationFailure(
                f"after the swap, top_hits/{name}/se differs from the decoded plane in {bad} cell(s)"
            )
    if _hm3_digest(plan.store_path / "data.zarr") != plan.hm3_before:
        raise VerificationFailure("the HM3 subset changed during the SE re-sync; it must not")


def _apply_resync(plan: ResyncPlan, encoding: StoreEncoding, workers: int) -> int:
    """Swap the verified arrays in, check the result, then finish the receipt."""
    record_path = paths.record_path(plan.store_id, plan.step, root=plan.artifact_root)
    _hash_live(plan)  # type: ignore[arg-type]
    record = _resync_started_record(plan, _precedent._utc_now())
    _write_receipt(record, record_path)

    try:
        _swap_all(plan, record, record_path, _fault_at())
        for staged in plan.staged:
            staged.sha256_after = _sha256_tree(staged.live_dir)
            if staged.sha256_after != staged.sha256_staged:
                raise VerificationFailure(
                    f"{staged.target.label} was swapped in but its files do not match the "
                    "verified staged copy"
                )
        _verify_live_se(plan, encoding, workers)
    except BaseException as exc:
        failures = _restore(plan, record, record_path)
        if failures:
            record["status"] = FAILED_RESTORATION
            record["failed_at"] = _precedent._utc_now()
            try:
                _write_receipt(record, record_path)
            except OSError as write_exc:
                failures.append(f"the receipt could not be finalised ({write_exc})")
            raise FlipRefusal(_failed_restoration_message(plan, record, exc, failures)) from exc
        for staged in plan.staged:
            shutil.rmtree(staged.staged_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            record_path.unlink()
        shutil.rmtree(plan.replaced_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            plan.replaced_dir.parent.rmdir()
        raise FlipRefusal(
            f"the SE re-sync of {plan.store_id} failed: {exc}. Every array is restored (proven by "
            "the recorded before-hashes), and this run's staged copies and receipt were removed."
        ) from exc

    by_label = {f"top_hits/{tier.tier}/se": tier for tier in plan.tiers}
    record["status"] = "complete"
    record["completed_at"] = _precedent._utc_now()
    record["hm3_sha256_after"] = _hm3_digest(plan.store_path / "data.zarr")
    record["arrays"] = [_resync_entry(staged, by_label[staged.target.label]) for staged in plan.staged]
    _write_receipt(record, record_path)

    print(_describe_resync(plan, dry_run=False))
    print(f"eaf-reference-flip: every top-hit SE now equals the decoded plane (rtol 0); HM3 subset untouched")
    print(f"eaf-reference-flip: originals kept in {plan.replaced_dir}")
    print(f"eaf-reference-flip: receipt {record_path}")
    return 0


def _resync_dry_run(
    args: argparse.Namespace, artifact_root: Path, store_path: Path, revision: str, version: str
) -> int:
    scratch = _scratch_dir()
    try:
        plan = _build_resync_plan(
            args.store_id, artifact_root, store_path, scratch / "data.zarr", revision, version, args.workers
        )
        if plan is None:
            print(_nothing_to_resync(store_path))
        else:
            print(_describe_resync(plan, dry_run=True))
        print("Dry run: nothing in the Store or its records was written.")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _nothing_to_resync(store_path: Path) -> str:
    manifest = _read_manifest(store_path)
    if not _se_is_residual(manifest):
        return (
            "eaf-reference-flip: this Store's SE is not an int8_residual plane, so it does not "
            "depend on eaf_reference and no top-hit SE needs re-syncing"
        )
    return "eaf-reference-flip: every top-hit SE already equals the decoded plane; nothing to re-sync"


def _resync_apply(
    args: argparse.Namespace, artifact_root: Path, store_path: Path, revision: str, version: str
) -> int:
    _precedent._configure_run(args.store_id, artifact_root, args.workers)
    plan = _build_resync_plan(
        args.store_id, artifact_root, store_path, store_path / "data.zarr", revision, version, args.workers
    )
    if plan is None:
        print(_nothing_to_resync(store_path))
        return 0
    encoding = StoreEncoding.from_manifest(_read_manifest(store_path)["encoding"])
    return _apply_resync(plan, encoding, args.workers)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-off, value-changing in-place correction of a Store's EAF orientation "
            "(#409): eaf_reference, its HM3 copy, and the eaf of imputed top hits"
        ),
        allow_abbrev=False,
    )
    parser.add_argument("store_id", help="one registered Store id (OGS-NNNNN)")
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="take the lock, run the guard, stage and verify in a scratch directory; change nothing",
    )
    parser.add_argument(
        "--resync-se",
        action="store_true",
        help=(
            "only the follow-on: re-sync top_hits/<tier>/se to the decoded SE plane of a Store "
            "this tool has already flipped (a flip runs it by itself)"
        ),
    )
    parser.add_argument(
        "--workers",
        type=_precedent._workers,
        default=DEFAULT_DECODE_WORKERS,
        help=(
            "processes the core's top-hit SE collector decodes row chunks with, each with "
            f"Zarr async width and Blosc threads of one (1..{_precedent.MAX_WORKERS}, "
            f"default {DEFAULT_DECODE_WORKERS}: #326's cap on concurrent decodes)"
        ),
    )
    parser.add_argument(
        "--sample-seed",
        type=int,
        default=DEFAULT_SAMPLE_SEED,
        help=(
            "the orientation guard's sample seed, recorded in the receipt "
            f"(default {DEFAULT_SAMPLE_SEED})"
        ),
    )
    parser.add_argument(
        "--artifact-root",
        default=None,
        help="workflow config override for the Artifact Root (default: ogstores.yaml/env)",
    )
    parser.add_argument(
        "--registry-root",
        default=None,
        help="directory of registered Release Bundles (default: <repo>/stores)",
    )
    return parser.parse_args(argv)


@contextlib.contextmanager
def _borrowed_precedent() -> Iterator[None]:
    """Run `se_reencode`'s refusals, as this command's own.

    The marker set and the runtime proof are the same statements #326 makes,
    and duplicating them here is how they drift apart: this command refuses on
    exactly what that one refuses on, by construction.
    """
    try:
        yield
    except _precedent.ReencodeRefusal as exc:
        raise FlipRefusal(str(exc)) from exc


def _scratch_dir() -> Path:
    """A staging directory outside the Store; the caller removes it."""
    return Path(tempfile.mkdtemp(prefix="eaf-reference-flip-"))


def _refuse_existing_receipts(
    store_id: str, artifact_root: Path, store_path: Path, *, resync_only: bool
) -> None:
    """Refuse on this operation's own receipts: the flip's and the follow-on's.

    The follow-on alone runs on a flipped Store, so the flip's receipt is
    required for it (checked when it is planned) and only its own receipt
    refuses here.
    """
    if not resync_only:
        _refuse_existing_receipt(store_id, artifact_root, store_path)
    _refuse_existing_receipt(
        store_id,
        artifact_root,
        store_path,
        step=RESYNC_STEP,
        suffix=RESYNC_REPLACED_SUFFIX,
        staging=RESYNC_STAGING_SUFFIX,
        consequence="This tool refuses: the SE re-sync runs once, and its receipt is evidence.",
    )


def _refuse_common(args: argparse.Namespace) -> tuple[Path, Path, str, str]:
    """Steps 1-2: the Store, the runtime and the markers, all before any write."""
    registry_root, artifact_root, store_path = _precedent._resolve_paths(args)
    with _borrowed_precedent():
        _precedent._refuse_store_outside_contract(args.store_id, registry_root, artifact_root, store_path)
        revision, version = _precedent._resolve_runtime()
        _precedent._refuse_markers(args.store_id, artifact_root)
    _refuse_existing_receipts(args.store_id, artifact_root, store_path, resync_only=args.resync_se)
    _refuse_out_of_scope(store_path, _read_manifest(store_path))
    return artifact_root, store_path, revision, version


def _with_lock(
    store_id: str,
    artifact_root: Path,
    store_path: Path,
    body: Callable[[], int],
    *,
    resync_only: bool = False,
) -> int:
    """Take the release's exclusive lock for `body`, never waiting for it.

    Apply and `--dry-run` both hold it: a dry run reads, stages and verifies
    the live arrays, and another writer must not change them underneath it.
    """
    locks = release.ReleaseLocks(artifact_root)
    try:
        locks.acquire(store_id)
    except release.ReleaseInProgressError as exc:
        print(f"eaf-reference-flip: {exc}", file=sys.stderr)
        return 1
    try:
        with _borrowed_precedent():
            _precedent._refuse_markers(store_id, artifact_root)
        _refuse_existing_receipts(store_id, artifact_root, store_path, resync_only=resync_only)
        return body()
    except (FlipRefusal, OSError) as exc:
        print(f"eaf-reference-flip: {exc}", file=sys.stderr)
        return 1
    finally:
        locks.release(store_id)


def _dry_run(args: argparse.Namespace, artifact_root: Path, store_path: Path, revision: str, version: str) -> int:
    scratch = _scratch_dir()
    try:
        plan = _build_plan(
            args.store_id,
            artifact_root,
            store_path,
            scratch / "data.zarr",
            args.sample_seed,
            revision,
            version,
        )
        print(_describe(plan, dry_run=True))
        if _se_is_residual(_read_manifest(store_path)):
            print(
                "  then, once the flip is applied, the same run re-syncs top_hits/<tier>/se to the "
                "decoded SE plane (its SE depends on eaf_reference); the cells it moves are "
                "counted by `--resync-se --dry-run` on a flipped Store"
            )
        print("Dry run: nothing in the Store or its records was written.")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _apply_plan(args: argparse.Namespace, artifact_root: Path, store_path: Path, revision: str, version: str) -> int:
    # `_configure_run` also pins Blosc to one thread, so the staged shards of
    # one slice are compressed the same way on every run; the async width only
    # affects how many inner chunks are decoded at once.
    _precedent._configure_run(args.store_id, artifact_root, _precedent.DEFAULT_WORKERS)
    plan = _build_plan(
        args.store_id,
        artifact_root,
        store_path,
        store_path / "data.zarr",
        args.sample_seed,
        revision,
        version,
    )
    code = _apply(plan)
    if code != 0 or not _se_is_residual(_read_manifest(store_path)):
        return code
    # The flip is complete and durable; the follow-on is its own operation with
    # its own receipt.  When it cannot finish, the Store stays flipped and the
    # operator is told how to finish it.
    try:
        return _resync_apply(args, artifact_root, store_path, revision, version)
    except (FlipRefusal, OSError) as exc:
        print(f"eaf-reference-flip: {exc}", file=sys.stderr)
        print(
            f"eaf-reference-flip: the flip of {args.store_id} is complete but its top-hit SE is "
            f"not re-synced; run `eaf-reference-flip {args.store_id} --resync-se` to finish",
            file=sys.stderr,
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        artifact_root, store_path, revision, version = _refuse_common(args)
    except FlipRefusal as exc:
        print(f"eaf-reference-flip: {exc}", file=sys.stderr)
        return 1

    if args.resync_se:
        body = _resync_dry_run if args.dry_run else _resync_apply
    else:
        body = _dry_run if args.dry_run else _apply_plan
    return _with_lock(
        args.store_id,
        artifact_root,
        store_path,
        lambda: body(args, artifact_root, store_path, revision, version),
        resync_only=args.resync_se,
    )


if __name__ == "__main__":
    raise SystemExit(main())
