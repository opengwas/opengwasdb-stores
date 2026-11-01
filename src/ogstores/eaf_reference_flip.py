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

Observed `eaf`, `eaf_baseline`, `z` and `se` are untouched: finishing is a
global allele flip whose only in-store consequence is those three arrays.

    pixi run eaf-reference-flip OGS-00014 --dry-run
    pixi run eaf-reference-flip OGS-00014

Safety, in the order it happens:

1. The release lock is taken (never waited for) after the same refusals
   `se_reencode` makes -- a registered id, a non-symlinked Store, no
   publication or completion in progress -- and the imported *and* PATH
   `opengwasdb` must both be the tracked pin.  A receipt that already exists
   refuses: `1 - (1 - x)` is `x`, so a second run silently restores the bug.
2. Only a Dense, format-0.2.0, Zarr-v3, `reference_completed` Store is in
   scope.  Ragged, Hybrid, Zarr v2 and Observed-Only Stores are refused with
   the reason, never skipped.
3. **Before touching anything**, the orientation guard: on a deterministic
   sample of variants that carry both a finite `eaf_reference` and an observed
   EAF in at least one Analysis, the Pearson r between the two must be
   `<= -0.9` over at least 500 variants.  Observed EAF is decoded through the
   runtime's own reader (`opengwasdb.encoding.DenseEafPlane`, the plane
   `StoreQuery` reads), never re-derived here.  The seed, r and n are recorded.
4. Each target is staged as a sibling `.<name>.flipping` directory: the live
   `zarr.json` is copied verbatim and whole shards are written through that
   metadata, so codecs and chunking cannot drift.  `float32(1) - x` is
   computed in float32 (the same operation on the same input is bit-identical
   everywhere, which is what `validate` requires of the copies) and only where
   the value is finite; a NaN keeps its exact payload.
5. The staged arrays are verified algebraically against the originals --
   `new == 1 - old` on finite cells as raw bit patterns, NaN positions
   unchanged, filtered rows bit-identical, and the HM3 copy equal to the staged
   primary at its variants.  The guard is then repeated on the staged primary
   and must read `r > 0.9`.  Only then is anything renamed.
6. The swap is `store/swap.py`'s pattern at directory level: the original array
   directory is moved into `<store-dir>/replaced/<UTC>-eaf-reference-flip/`
   (never inside the Store), then the staged directory is moved into its name.
   A failure between the two renames puts the original back; a receipt already
   marked `started` is recovery evidence naming the rollback directory.
7. `records/eaf-reference-flip.json` is written `started` immediately before
   the first rename and `complete` after the last: per-touched-array sha256 of
   every file before and after, the flipped/NaN counts, the guard before and
   after, the core revision and the tool revision (the shape
   `tools/relocate_store.py` uses).

`--dry-run` performs steps 1-5 into a scratch directory outside the Store,
prints the plan and the counts, and changes nothing in the Store or its
records.  The tool never touches a Store-root file (`manifest.json`,
`index.sqlite`, `overview.html`, ...) or any array it does not name.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

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

#: The stamp form `replaced/` uses, and the one `se_reencode`/`complete` use.
REPLACED_SUFFIX: str = "eaf-reference-flip"

#: The guard's deterministic sample: bands of consecutive variants, `MAX_BANDS`
#: of them at most, so a 30M-variant Store costs a bounded number of band
#: decodes (the exploration behind #409 used 60 bands of 200 rows).
BAND_ROWS: int = 200
MAX_BANDS: int = 60

#: The issue's evidence floor: fewer comparable variants than this cannot show
#: the orientation at all, and a run must not guess.
MIN_COMPARABLE: int = 500

#: An inverted Store reads strongly negative; a corrected one strongly
#: positive.  The margins are loose on purpose: the measurement is a
#: correlation over real frequencies, not an identity.
BEFORE_MAX_R: float = -0.9
AFTER_MIN_R: float = 0.9

#: The default sample seed.  Recorded in the receipt, overridable so a re-run of
#: the evidence can draw another sample.
DEFAULT_SAMPLE_SEED: int = 20261101

#: Test-only fault points, after `run.fault_boundary` and relocate_store's
#: variables: raise at a named step so a test can prove nothing is swapped.
FAIL_AT_ENV: str = "OGSTORES_TEST_EAF_REFERENCE_FLIP_FAIL_AT"
VERIFY_FAULT, SWAP_FAULT = "verify", "swap"

#: Environment variables a real run keeps on the artifact root (CONTRIBUTING).
_TMPDIR_VARIABLES: tuple[str, ...] = ("TMPDIR", "TMP", "TEMP")

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


def _utc_now() -> str:
    """The UTC timestamp form the repository's records use."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_stamp() -> str:
    """The stamp form `replaced/` uses: `YYYYMMDDTHHMMSSZ`."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
    Store this issue describes.
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
            if "eaf" in tier:
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
    the observed EAF and nothing re-derived from `eaf_reference`.
    """
    n_variants = int(root["eaf_reference"].shape[0])
    plane = DenseEafPlane.open(root, encoding)
    rows: list[np.ndarray] = []
    observed: list[np.ndarray] = []
    bands = _sampled_bands(n_variants, seed)
    band_rows = min(BAND_ROWS, n_variants)
    for r0, r1 in bands:
        read = plane.read_band(r0, r1, want_imputed=True)
        values = np.asarray(read.values, dtype=np.float32)
        if read.imputed is None:
            mask = np.zeros(values.shape, dtype=bool)
        else:
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
    if guard.r is None or guard.r > BEFORE_MAX_R:
        raise FlipRefusal(
            "the orientation guard is not inverted: expected r <= "
            f"{BEFORE_MAX_R} between eaf_reference and the observed EAF, got {guard.describe()}. "
            "This Store does not need (or has already had) this correction."
        )


def _require_corrected(guard: Guard) -> None:
    """The post-condition, on the staged primary: the flip must have worked."""
    if guard.r is None or guard.r < AFTER_MIN_R:
        raise VerificationFailure(
            "the orientation guard on the staged primary did not read corrected: "
            f"expected r >= {AFTER_MIN_R}, got {guard.describe()}"
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
    float64 would send `0.99` back as `0.010000000707...` and break that.
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
    everything else (NaNs included) is untouched.  For a filtered tier the
    unflipped rows must be bit-identical, which the same comparison covers.
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
# The swap
# ---------------------------------------------------------------------------


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _swap_in(staged: StagedArray, rollback_dir: Path, fault: str | None) -> dict[str, Any]:
    """Move the original aside and the staged array into its name, or put it back."""
    rollback = rollback_dir / staged.target.group / staged.target.array
    rollback.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(rollback):
        raise FlipRefusal(f"the rollback destination {rollback} already exists")
    if fault == SWAP_FAULT:
        raise FlipRefusal(f"test fault: refusing before the swap of {staged.target.label} ({FAIL_AT_ENV})")
    os.replace(staged.live_dir, rollback)
    _fsync_dir(staged.live_dir.parent)
    try:
        os.replace(staged.staged_dir, staged.live_dir)
    except BaseException:
        os.replace(rollback, staged.live_dir)
        _fsync_dir(staged.live_dir.parent)
        raise
    _fsync_dir(staged.live_dir.parent)
    return {
        "array": staged.target.label,
        "original": str(rollback),
        "staged": str(staged.live_dir),
        "flipped": str(staged.staged_dir),
    }


# ---------------------------------------------------------------------------
# The plan and the receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """Everything the correction will do, measured before it does anything."""

    store_id: str
    artifact_root: Path
    store_path: Path
    data_zarr: Path
    encoding: StoreEncoding
    targets: list[Target]
    staged: list[StagedArray]
    before: Guard
    after: Guard
    revision: str
    version: str
    replaced_dir: Path
    stamp: str


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
        "sha256_after": staged.sha256_after,
        "verified": "new == 1 - old on finite cells (raw float32 bits); "
        "NaN positions and unflipped rows bit-identical",
    }


def _started_record(plan: Plan, started_at: str, swap_plan: list[dict[str, Any]]) -> dict[str, Any]:
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
        "replaced": {"dir": str(plan.replaced_dir), "arrays": swap_plan},
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

    stamp = f"{_utc_stamp()}-{REPLACED_SUFFIX}"
    return Plan(
        store_id=store_id,
        artifact_root=artifact_root,
        store_path=store_path,
        data_zarr=data_zarr,
        encoding=encoding,
        targets=targets,
        staged=staged,
        before=before,
        after=after,
        revision=revision,
        version=version,
        replaced_dir=paths.replaced_dir(store_id, stamp, root=artifact_root),
        stamp=stamp,
    )


def _hash_live(plan: Plan) -> None:
    """Record each touched array's per-file sha256, before the swap."""
    for staged in plan.staged:
        staged.sha256_before = _sha256_tree(staged.live_dir)
        staged.sha256_staged = _sha256_tree(staged.staged_dir)


def _refuse_existing_receipt(store_id: str, artifact_root: Path) -> None:
    """A receipt of any status refuses: `1 - (1 - x)` is `x` (issue #409)."""
    record_path = paths.record_path(store_id, RECORD_STEP, root=artifact_root)
    if not os.path.lexists(record_path):
        return
    status = "unreadable"
    if record_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            status = str(json.loads(record_path.read_text(encoding="utf-8")).get("status"))
    raise FlipRefusal(
        f"{record_path} already exists (status {status!r}). A second flip would restore "
        "the inversion this one corrects, so this tool refuses. Restore the arrays from "
        "`replaced/` and remove the receipt only if the first run is known not to have "
        "landed."
    )


def _write_receipt(record: dict[str, Any], record_path: Path) -> None:
    run._write_record_atomically(record, record_path)


def _apply(plan: Plan) -> int:
    """Swap the verified arrays in, then finish the receipt.  Steps 5-6."""
    record_path = paths.record_path(plan.store_id, RECORD_STEP, root=plan.artifact_root)
    _hash_live(plan)
    started_at = _utc_now()
    swap_plan = [
        {
            "array": staged.target.label,
            "original": str(plan.replaced_dir / staged.target.group / staged.target.array),
            "staged": str(staged.live_dir),
        }
        for staged in plan.staged
    ]
    record = _started_record(plan, started_at, swap_plan)
    _write_receipt(record, record_path)

    moved: list[dict[str, Any]] = []
    try:
        for staged in plan.staged:
            moved.append(_swap_in(staged, plan.replaced_dir, _fault_at()))
    except BaseException as exc:
        # Put back whatever already moved, and take the started receipt away:
        # the Store is as it was, so blocking a later run would be a lie.
        restored, failed = [], []
        for entry in reversed(moved):
            rollback = Path(entry["original"])
            live = Path(entry["staged"])
            flipped = Path(entry["flipped"])
            try:
                if os.path.lexists(rollback):
                    if os.path.lexists(live):
                        # The corrected copy goes back to its staging name first:
                        # the live name must be free for the original.
                        os.replace(live, flipped)
                    os.replace(rollback, live)
                    shutil.rmtree(flipped, ignore_errors=True)
                    _fsync_dir(live.parent)
                    restored.append(entry["array"])
            except OSError as restore_exc:  # pragma: no cover - needs a broken filesystem
                failed.append(f"{entry['array']}: {restore_exc}")
        # Whatever was staged and did not land goes too: the arrays that were
        # never swapped still have their staged copies in the Store.
        for staged in plan.staged:
            shutil.rmtree(staged.staged_dir, ignore_errors=True)
        if not failed:
            with contextlib.suppress(OSError):
                record_path.unlink()
            shutil.rmtree(plan.replaced_dir, ignore_errors=True)
        state = (
            f"Restored {', '.join(restored)} from {plan.replaced_dir}."
            if restored else "No array had moved."
        )
        if failed:
            state += (
                f" Could not restore {', '.join(failed)}; {record_path} names the rollback "
                "directory."
            )
        raise FlipRefusal(f"the swap of {plan.store_id} failed: {exc}. {state}") from exc

    for staged in plan.staged:
        staged.sha256_after = _sha256_tree(staged.live_dir)
        if staged.sha256_after != staged.sha256_staged:
            raise FlipRefusal(
                f"{staged.target.label} was swapped in but its files do not match the "
                f"verified staged copy (see {record_path}); restore it from {plan.replaced_dir}"
            )

    record["status"] = "complete"
    record["completed_at"] = _utc_now()
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
        help="run the guard, stage and verify in a scratch directory; change nothing",
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


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    """Resolve registry root, artifact root and the published Store path via ogstores."""
    registry_root = Path(args.registry_root) if args.registry_root else paths.REPO_ROOT / "stores"
    if not registry_root.is_absolute():
        registry_root = paths.REPO_ROOT / registry_root
    artifact_root = paths.artifact_root(config_override=args.artifact_root)
    store_path = paths.store_path(args.store_id, root=artifact_root)
    return registry_root, artifact_root, store_path


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


def _configure_run(store_id: str, artifact_root: Path) -> None:
    """Point temp files at the artifact root: the Store's arrays are on /data."""
    tmp = paths.tmp_dir(store_id, root=artifact_root)
    tmp.mkdir(parents=True, exist_ok=True)
    for variable in _TMPDIR_VARIABLES:
        os.environ[variable] = str(tmp)


def _scratch_dir() -> Path:
    """A staging directory outside the Store; the caller removes it."""
    return Path(tempfile.mkdtemp(prefix="eaf-reference-flip-"))


def _refuse_common(args: argparse.Namespace) -> tuple[Path, Path, Path, str, str]:
    """Steps 1-2: the Store, the runtime and the markers, all before any write."""
    registry_root, artifact_root, store_path = _resolve_paths(args)
    with _borrowed_precedent():
        _precedent._refuse_store_outside_contract(
            args.store_id, registry_root, artifact_root, store_path
        )
        revision, version = _precedent._resolve_runtime()
        _precedent._refuse_markers(args.store_id, artifact_root)
    _refuse_existing_receipt(args.store_id, artifact_root)
    return artifact_root, store_path, registry_root, revision, version


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        artifact_root, store_path, _registry_root, revision, version = _refuse_common(args)
        _refuse_out_of_scope(store_path, _read_manifest(store_path))
    except FlipRefusal as exc:
        print(f"eaf-reference-flip: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        holder = release.lock_holder(args.store_id, artifact_root)
        if holder is not None:
            print(
                f"eaf-reference-flip: a run of {args.store_id} is in progress ({holder}); "
                "the real run would refuse",
                file=sys.stderr,
            )
            return 1
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
        except FlipRefusal as exc:
            print(f"eaf-reference-flip: {exc}", file=sys.stderr)
            return 1
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        print(_describe(plan, dry_run=True))
        print("Dry run: nothing in the Store or its records was written.")
        return 0

    locks = release.ReleaseLocks(artifact_root)
    try:
        locks.acquire(args.store_id)
    except release.ReleaseInProgressError as exc:
        print(f"eaf-reference-flip: {exc}", file=sys.stderr)
        return 1
    try:
        # Re-check under the held lock, and stage beside the live arrays so the
        # swap is two renames on one filesystem.
        with _borrowed_precedent():
            _precedent._refuse_markers(args.store_id, artifact_root)
        _refuse_existing_receipt(args.store_id, artifact_root)
        _configure_run(args.store_id, artifact_root)
        try:
            plan = _build_plan(
                args.store_id,
                artifact_root,
                store_path,
                store_path / "data.zarr",
                args.sample_seed,
                revision,
                version,
            )
        except FlipRefusal as exc:
            print(f"eaf-reference-flip: {exc}", file=sys.stderr)
            return 1
        try:
            return _apply(plan)
        except FlipRefusal as exc:
            print(f"eaf-reference-flip: {exc}", file=sys.stderr)
            return 1
    finally:
        locks.release(args.store_id)


if __name__ == "__main__":
    raise SystemExit(main())
