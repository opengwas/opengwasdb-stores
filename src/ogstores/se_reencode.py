"""One supported maintenance command: lossless SE re-encode for one Store (#326).

Every published format-0.2.0 Dense Store stores its `se` plane (int8) and
`se_exception_index` (int64) with a Blosc `typesize` of 4, inherited from the
float32 scratch plane the SE rewrite staged. Bitshuffling int8 codes as 4-byte
elements defeats the shuffle filter. The owner's decision (2026-10-24) is to
fix the seven existing Stores in place, once, as a pre-release exception:
the change is physical representation only, and every decoded value is proven
equal across the full array before the swap.

This module is the registry/orchestration slice. It does not re-encode bytes
itself; it resolves the artifact root and published Store through `ogstores`
(never a caller-guessed sibling path), takes the authoritative exclusive
`ReleaseLocks` lock non-blockingly, refuses a release transaction or
completion in progress before any write, then calls the reviewed core
primitive (`opengwasdb.reencode.plan_se_reencode` /
`opengwasdb.reencode.reencode_se_codecs`) and writes the checked record
`records/se-reencode.json` from the returned plans/results.

    pixi run se-reencode OGS-00009
    pixi run se-reencode OGS-00009 --dry-run
    pixi run se-reencode OGS-00009 --workers 8 --artifact-root /path

The command is idempotent: a second run of an already-correct Store is a
no-op, writes no record, and reports "already correct". A dry run is strictly
read-only: it takes no lock, creates no directory, and writes nothing.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

import numcodecs.blosc
from opengwasdb.reencode import plan_se_reencode as _core_plan
from opengwasdb.reencode import reencode_se_codecs as _core_apply

from ogstores import paths, release, run, runtime

#: The step name under `records/`, and so the record's file name.
RECORD_STEP: str = "se-reencode"

#: The issue's host-resource cap: the re-encode runs at most this many workers
#: wide. The core writes one array at a time through zarr's fused pipeline with
#: one worker (opengwasdb ADR 0056); the only thread knob a re-encode exposes
#: is Blosc's internal thread count, so "workers" is applied there.
DEFAULT_WORKERS: int = 16
MAX_WORKERS: int = 16

#: Environment variables a run keeps on the artifact root's filesystem, not
#: `/tmp` (CONTRIBUTING; issue #326). The core stages its swap beside the live
#: array, but zarr/numcodecs may still spill, so a run is never allowed to
#: default `TMPDIR` back to the root filesystem.
_TMPDIR_VARIABLES: tuple[str, ...] = ("TMPDIR", "TMP", "TEMP")


class ReencodeRefusal(RuntimeError):
    """The command refuses to run; nothing of the Store was written."""


def _utc_now() -> str:
    """The UTC timestamp form the repository's Step records use."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _workers(value: str) -> int:
    """Read `--workers`, refusing anything outside 1..MAX_WORKERS (#326 cap)."""
    try:
        workers = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"workers must be an integer, not {value!r}"
        ) from None
    if workers < 1 or workers > MAX_WORKERS:
        raise argparse.ArgumentTypeError(
            f"workers must be between 1 and {MAX_WORKERS}, not {workers}"
        )
    return workers


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the operator's words; the cap and the single-Store shape are enforced here."""
    parser = argparse.ArgumentParser(
        description="Lossless SE re-encode for one registered Store Release (#326)",
        allow_abbrev=False,
    )
    parser.add_argument("store_id", help="one registered Store id (OGS-NNNNN)")
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="plan only; report what would change and write nothing",
    )
    parser.add_argument(
        "--workers",
        type=_workers,
        default=DEFAULT_WORKERS,
        help=f"Blosc worker threads for the re-encode (1..{MAX_WORKERS}, default {DEFAULT_WORKERS})",
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


def _refuse_store_outside_contract(
    store_id: str, registry_root: Path, artifact_root: Path, store_path: Path
) -> None:
    """Refuse an id that is not registered, or a Store outside the artifact-root contract."""
    try:
        paths.require_valid_store_id(store_id)
    except ValueError as exc:
        raise ReencodeRefusal(str(exc)) from exc
    if store_id not in release.registered_ids(registry_root):
        raise ReencodeRefusal(
            f"{store_id} is not a registered Release Bundle under {registry_root}"
        )
    if store_path.is_symlink():
        raise ReencodeRefusal(
            f"{store_path} is a symlink; refusing to re-encode through it"
        )
    if not store_path.is_dir():
        raise ReencodeRefusal(f"no published Store at {store_path}")


def _refuse_markers(store_id: str, artifact_root: Path) -> None:
    """Refuse a run while a release transaction or completion is in progress.

    These are existence checks, not lock checks: a marker under a *live* run's
    lock is that run's business, and a marker with no lock is a crash leftover
    that the release entry point, not the re-encode, settles. Either way the
    re-encode must not write beside it.
    """
    markers = [
        paths.publication_marker(store_id, root=artifact_root),
        paths.partial_store_path(store_id, root=artifact_root),
        paths.completion_start_path(store_id, root=artifact_root),
        paths.completion_checkpoint_path(store_id, root=artifact_root),
    ]
    present = [str(marker) for marker in markers if os.path.lexists(marker)]
    if present:
        raise ReencodeRefusal(
            f"{store_id} has a release transaction or completion in progress; "
            "nothing was written. Wait for it to finish and settle:\n"
            + "\n".join(f"  {marker}" for marker in present)
        )


def _resolve_runtime() -> tuple[str, str]:
    """Resolve and refuse any opengwasdb that is not the tracked, provable pin.

    The record's revision must be the exact core revision that ran, so an
    unprovable or different identity is refused rather than recorded.
    """
    executable = run.get_opengwasdb_executable()
    if executable == run.UNAVAILABLE:
        raise ReencodeRefusal(
            "no opengwasdb executable was found on PATH. Run through "
            "`pixi run se-reencode` so the pinned runtime is used."
        )
    revision = run.get_opengwasdb_revision(executable=executable)
    version = run.get_opengwasdb_version(executable=executable)
    if not run.is_exact_commit_hash(revision) or revision != runtime.PINNED_OPENGWASDB_REVISION:
        raise ReencodeRefusal(
            f"the resolved opengwasdb at {executable!r} reports revision {revision!r}, "
            f"which is not the tracked pin {runtime.PINNED_OPENGWASDB_REVISION} "
            f"({runtime.OPENGWASDB_GIT_URL}). Run `pixi install` so the pinned runtime "
            "is used; never substitute an executable on PATH."
        )
    return revision, version


def _dry_run_report(
    store_id: str,
    artifact_root: Path,
    store_path: Path,
    revision: str,
    workers: int,
    plans: list,
) -> str:
    """The read-only plan, with the facts the issue's dry run must report."""
    lines = [
        f"se-reencode dry run for {store_id}",
        f"artifact root: {artifact_root}",
        f"store: {store_path}",
        f"core revision: {revision}",
        f"workers: {workers}",
    ]
    if not plans:
        lines.append("nothing to re-encode; the Store is already correct")
        return "\n".join(lines)
    lines.append(f"would re-encode {len(plans)} array(s):")
    for plan in plans:
        lines.append(
            f"  {plan.group}/{plan.array}  dtype={plan.dtype}  "
            f"typesize {plan.current_typesize} -> {plan.expected_typesize}  "
            f"before={plan.before_bytes} bytes  "
            f"estimated_after={plan.estimated_after_bytes} bytes  "
            f"estimated_saving={plan.estimated_saving_bytes} bytes"
        )
    return "\n".join(lines)


def _set_run_environment(store_id: str, artifact_root: Path, workers: int) -> None:
    """Point temp files at the artifact root and cap Blosc at the requested workers."""
    tmp = paths.tmp_dir(store_id, root=artifact_root)
    tmp.mkdir(parents=True, exist_ok=True)
    for variable in _TMPDIR_VARIABLES:
        # A dry run never reaches here, so creating the directory is fine.
        os.environ[variable] = str(tmp)
    numcodecs.blosc.set_nthreads(workers)


def _record(results: list, *, store_id: str, revision: str, version: str, start: str, end: str, complete: bool) -> dict:
    """The checked record: this run's per-array change, with the completeness verdict."""
    return {
        "step": RECORD_STEP,
        "store_id": store_id,
        "opengwasdb_revision": revision,
        "opengwasdb_version": version,
        "start_time": start,
        "end_time": end,
        "complete": complete,
        "arrays": [
            {
                "group": result.group,
                "array": result.array,
                "before_bytes": result.before_bytes,
                "after_bytes": result.after_bytes,
                "saved_bytes": result.before_bytes - result.after_bytes,
                "decoded_equality": result.equality,
                "chunks": result.chunks,
            }
            for result in results
        ],
    }


def _apply(
    store_id: str,
    artifact_root: Path,
    store_path: Path,
    workers: int,
    revision: str,
    version: str,
) -> int:
    """Run the core primitive and write the checked record, atomically."""
    _set_run_environment(store_id, artifact_root, workers)
    start = _utc_now()
    try:
        results = _core_apply(store_path)
    except Exception as exc:
        raise ReencodeRefusal(
            f"se-reencode of {store_id} failed; no success record was written: {exc}"
        ) from exc
    end = _utc_now()

    if not results:
        print(f"se-reencode: {store_id} is already correct; nothing to do")
        return 0

    try:
        remaining = _core_plan(store_path)
    except Exception as exc:
        raise ReencodeRefusal(
            f"se-reencode of {store_id} applied but could not be re-planned: {exc}"
        ) from exc
    record = _record(
        results,
        store_id=store_id,
        revision=revision,
        version=version,
        start=start,
        end=end,
        complete=not remaining,
    )
    run._write_record_atomically(record, paths.record_path(store_id, RECORD_STEP, root=artifact_root))

    lines = [
        f"se-reencode: re-encoded {len(results)} array(s) of {store_id}",
        *(
            f"  {result.group}/{result.array}: {result.before_bytes} -> {result.after_bytes} bytes "
            f"(saved {result.before_bytes - result.after_bytes}), decoded equal: {result.equality}, "
            f"{result.chunks} chunks"
            for result in results
        ),
        f"se-reencode: wrote {paths.record_path(store_id, RECORD_STEP, root=artifact_root)}",
    ]
    if not remaining:
        lines.append(f"se-reencode: {store_id} is now fully re-encoded")
    else:
        lines.append(
            f"se-reencode: {store_id} is partial; still needing re-encode: "
            + ", ".join(f"{p.group}/{p.array}" for p in remaining)
        )
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        registry_root, artifact_root, store_path = _resolve_paths(args)
        _refuse_store_outside_contract(args.store_id, registry_root, artifact_root, store_path)
        revision, version = _resolve_runtime()
        _refuse_markers(args.store_id, artifact_root)
    except ReencodeRefusal as exc:
        print(f"se-reencode: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        holder = release.lock_holder(args.store_id, artifact_root)
        if holder is not None:
            print(
                f"se-reencode: a run of {args.store_id} is in progress ({holder}); "
                "the real run would refuse",
                file=sys.stderr,
            )
            return 1
        try:
            plans = _core_plan(store_path)
        except Exception as exc:
            print(f"se-reencode: cannot plan a re-encode of {store_path}: {exc}", file=sys.stderr)
            return 1
        print(_dry_run_report(args.store_id, artifact_root, store_path, revision, args.workers, plans))
        return 0

    locks = release.ReleaseLocks(artifact_root)
    try:
        locks.acquire(args.store_id)
    except release.ReleaseInProgressError as exc:
        print(f"se-reencode: {exc}", file=sys.stderr)
        return 1
    try:
        # Re-check under the held lock: a marker could not appear without its
        # run holding the lock, so this closes the read-only check's race.
        _refuse_markers(args.store_id, artifact_root)
        return _apply(args.store_id, artifact_root, store_path, args.workers, revision, version)
    except ReencodeRefusal as exc:
        print(f"se-reencode: {exc}", file=sys.stderr)
        return 1
    finally:
        locks.release(args.store_id)


if __name__ == "__main__":
    raise SystemExit(main())
