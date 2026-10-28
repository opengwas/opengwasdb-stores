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
import json
import os
import sys
from pathlib import Path

import numcodecs.blosc
import zarr
from opengwasdb.reencode import plan_se_reencode as _core_plan
from opengwasdb.reencode import reencode_se_codecs as _core_apply

from ogstores import paths, release, run, runtime

#: The step name under `records/`, and so the record's file name.
RECORD_STEP: str = "se-reencode"

#: The issue's host-resource cap: the re-encode runs at most this many workers
#: wide. The one-shot process's Zarr async codec-pipeline width is set to this
#: value; Blosc's internal threads are pinned single so the width is exactly
#: `workers` Zarr workers, never `workers` x `workers` threads.
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
        help=(
            f"Zarr codec-pipeline workers for the one-shot re-encode "
            f"(1..{MAX_WORKERS}, default {DEFAULT_WORKERS}); Blosc threads are "
            "pinned to 1 to avoid nested oversubscription"
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
    """Prove the PATH executable and the in-process distribution are both the pin.

    The record's revision must be the exact core revision that ran, and the
    core primitive is imported in-process while the operator could have a
    different `opengwasdb` on PATH.  Both identities are resolved and must be
    the immutable pin and each other; an unprovable or different identity is
    refused before any Store write.
    """
    executable = run.get_opengwasdb_executable()
    if executable == run.UNAVAILABLE:
        raise ReencodeRefusal(
            "no opengwasdb executable was found on PATH. Run through "
            "`pixi run se-reencode` so the pinned runtime is used."
        )
    executable_revision = run.get_opengwasdb_revision(executable=executable)
    ambient_revision = run.get_opengwasdb_revision()
    version = run.get_opengwasdb_version(executable=executable)

    if not run.is_exact_commit_hash(executable_revision):
        raise ReencodeRefusal(
            f"cannot prove the opengwasdb executable revision at {executable!r} "
            f"(got {executable_revision!r}); an unprovable identity is refused before "
            "any Store write."
        )
    if not run.is_exact_commit_hash(ambient_revision):
        raise ReencodeRefusal(
            f"cannot prove the imported opengwasdb distribution revision "
            f"(got {ambient_revision!r}); an unprovable identity is refused before "
            "any Store write."
        )
    if executable_revision != ambient_revision:
        raise ReencodeRefusal(
            f"the opengwasdb executable at {executable!r} reports revision "
            f"{executable_revision!r} but the imported distribution reports "
            f"{ambient_revision!r}; they must be the same pinned runtime."
        )
    if executable_revision != runtime.PINNED_OPENGWASDB_REVISION:
        raise ReencodeRefusal(
            f"the resolved opengwasdb runtime reports revision {executable_revision!r}, "
            f"which is not the tracked pin {runtime.PINNED_OPENGWASDB_REVISION} "
            f"({runtime.OPENGWASDB_GIT_URL}). Run `pixi install` so the pinned runtime "
            "is used; never substitute an executable on PATH."
        )
    return executable_revision, version


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
        f"workers: {workers} (Zarr codec-pipeline workers; Blosc single-threaded)",
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


def _apply_worker_config(workers: int) -> None:
    """Set the one-shot process's Zarr async width and keep Blosc single-threaded.

    zarr-python 3 resolves `codec_pipeline.max_workers` per codec-pipeline
    operation, so this is the actual async concurrency of the re-encode's
    chunk encodes/decodes.  Blosc's internal threads are a second, nested
    level, so they are pinned to one: `--workers` selects the Zarr width,
    never a multiplication of workers x threads (#326's "at most 16" cap).
    """
    zarr.config.set({"codec_pipeline.max_workers": workers})
    numcodecs.blosc.set_nthreads(1)


def _configure_run(store_id: str, artifact_root: Path, workers: int) -> None:
    """Point temp files at the artifact root and apply the worker cap."""
    tmp = paths.tmp_dir(store_id, root=artifact_root)
    tmp.mkdir(parents=True, exist_ok=True)
    for variable in _TMPDIR_VARIABLES:
        # A dry run never reaches here, so creating the directory is fine.
        os.environ[variable] = str(tmp)
    _apply_worker_config(workers)


def _record(
    results: list,
    *,
    store_id: str,
    revision: str,
    version: str,
    start: str,
    end: str,
    complete: bool,
    verification_error: str | None = None,
) -> dict:
    """The checked record: this run's per-array change, with the completeness verdict."""
    record = {
        "step": RECORD_STEP,
        "store_id": store_id,
        "opengwasdb_revision": revision,
        "opengwasdb_version": version,
        "start_time": start,
        "end_time": end,
        "complete": complete,
        "verification": (
            {"ok": False, "stage": "replan", "error": verification_error}
            if verification_error is not None
            else {"ok": True, "stage": "replan"}
        ),
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
    return record


def _load_existing_record(store_id: str, artifact_root: Path) -> dict | None:
    """Read the existing se-reencode record, or None when none exists.

    A present but unreadable or non-mapping record is refused: it is recovery
    evidence, so a wrong record that looks right must not be silently ignored.
    """
    record_path = paths.record_path(store_id, RECORD_STEP, root=artifact_root)
    if not record_path.is_file():
        return None
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReencodeRefusal(
            f"cannot read the existing record {record_path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ReencodeRefusal(
            f"the existing record {record_path} is not a JSON object; refusing to trust it"
        )
    return data


def _validate_incomplete_record(existing: dict, store_id: str, revision: str) -> None:
    """Refuse an incomplete record whose shape, id or revision cannot be trusted."""
    if existing.get("step") != RECORD_STEP:
        raise ReencodeRefusal(
            f"the existing record is not a {RECORD_STEP} record "
            f"(step={existing.get('step')!r})"
        )
    if existing.get("store_id") != store_id:
        raise ReencodeRefusal(
            f"the existing record's store_id {existing.get('store_id')!r} does not match {store_id}"
        )
    if existing.get("opengwasdb_revision") != revision:
        raise ReencodeRefusal(
            f"the existing record's opengwasdb_revision "
            f"{existing.get('opengwasdb_revision')!r} does not match the resolved runtime {revision}"
        )
    if existing.get("complete") is not False:
        raise ReencodeRefusal(
            "the existing record is not incomplete (complete is not false); "
            "refusing to finalize it"
        )
    verification = existing.get("verification")
    if not isinstance(verification, dict) or verification.get("ok") is not False:
        raise ReencodeRefusal(
            "the existing record has no failed verification to recover"
        )
    if not isinstance(existing.get("start_time"), str) or not existing.get("start_time"):
        raise ReencodeRefusal("the existing record has no usable start_time to preserve")
    arrays = existing.get("arrays")
    if not isinstance(arrays, list) or not arrays:
        raise ReencodeRefusal("the existing record has no arrays to preserve")
    for entry in arrays:
        if not isinstance(entry, dict):
            raise ReencodeRefusal("an array entry in the existing record is not an object")
        for key in ("group", "array"):
            if not isinstance(entry.get(key), str) or not entry.get(key):
                raise ReencodeRefusal(f"an array entry has no usable {key!r}")
        for key in ("before_bytes", "after_bytes", "saved_bytes", "chunks"):
            value = entry.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ReencodeRefusal(f"an array entry has invalid {key!r}: {value!r}")
        if not isinstance(entry.get("decoded_equality"), bool):
            raise ReencodeRefusal(
                f"an array entry has invalid decoded_equality: {entry.get('decoded_equality')!r}"
            )


def _finalize_incomplete_record(
    existing: dict, store_id: str, artifact_root: Path
) -> None:
    """Atomically finalize a verified incomplete record, preserving its evidence.

    The arrays (before/after/saved/chunks), the original start_time and the
    runtime identity stay as the prior apply wrote them; only `complete`,
    `verification` and `end_time` are updated to the recovered verdict.
    """
    finalized = dict(existing)
    finalized["complete"] = True
    finalized["verification"] = {"ok": True, "stage": "replan"}
    finalized["end_time"] = _utc_now()
    record_path = paths.record_path(store_id, RECORD_STEP, root=artifact_root)
    try:
        run._write_record_atomically(finalized, record_path)
    except OSError as exc:
        raise ReencodeRefusal(
            f"{store_id} is correct but finalizing the incomplete record "
            f"{record_path} failed: {exc}"
        ) from exc
    print(
        f"se-reencode: {store_id} recovered an incomplete record and finalized it "
        f"(complete=true, verification ok, arrays preserved)"
    )


def _apply(
    store_id: str,
    artifact_root: Path,
    store_path: Path,
    workers: int,
    revision: str,
    version: str,
) -> int:
    """Run the core primitive and write the checked record, atomically."""
    _configure_run(store_id, artifact_root, workers)
    start = _utc_now()
    try:
        results = _core_apply(store_path)
    except Exception as exc:
        raise ReencodeRefusal(
            f"se-reencode of {store_id} failed; no success record was written: {exc}"
        ) from exc
    end = _utc_now()

    if not results:
        existing = _load_existing_record(store_id, artifact_root)
        if existing is None or existing.get("complete") is True:
            print(f"se-reencode: {store_id} is already correct; nothing to do")
            return 0
        # An incomplete record exists. Trust it only after its shape, id and
        # revision check out, and only if a fresh plan proves the Store is now
        # correct, so a no-op never invents or trusts a stale verdict.
        _validate_incomplete_record(existing, store_id, revision)
        try:
            remaining = _core_plan(store_path)
        except Exception as exc:
            raise ReencodeRefusal(
                f"{store_id} changed nothing but could not be re-planned to confirm "
                f"correctness: {exc}"
            ) from exc
        if remaining:
            raise ReencodeRefusal(
                f"{store_id} still needs re-encode "
                f"({', '.join(f'{p.group}/{p.array}' for p in remaining)}) but "
                "reencode_se_codecs changed nothing; refusing to finalize"
            )
        _finalize_incomplete_record(existing, store_id, artifact_root)
        return 0

    verification_error: str | None = None
    complete = False
    remaining: list | None = None
    try:
        remaining = _core_plan(store_path)
        complete = not remaining
    except Exception as exc:
        verification_error = str(exc)

    record = _record(
        results,
        store_id=store_id,
        revision=revision,
        version=version,
        start=start,
        end=end,
        complete=complete,
        verification_error=verification_error,
    )
    record_path = paths.record_path(store_id, RECORD_STEP, root=artifact_root)
    try:
        run._write_record_atomically(record, record_path)
    except OSError as exc:
        print(
            f"se-reencode: {store_id} was re-encoded ({len(results)} array(s) changed) "
            f"but writing the record {record_path} failed: {exc}. The Store changed; "
            "the record failed.",
            file=sys.stderr,
        )
        return 1

    if verification_error is not None:
        print(
            f"se-reencode: {store_id} was re-encoded ({len(results)} array(s) changed) "
            f"and recorded complete=false, but the follow-up plan failed: "
            f"{verification_error}.",
            file=sys.stderr,
        )
        return 1

    lines = [
        f"se-reencode: re-encoded {len(results)} array(s) of {store_id}",
        *(
            f"  {result.group}/{result.array}: {result.before_bytes} -> {result.after_bytes} bytes "
            f"(saved {result.before_bytes - result.after_bytes}), decoded equal: {result.equality}, "
            f"{result.chunks} chunks"
            for result in results
        ),
        f"se-reencode: wrote {record_path}",
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
