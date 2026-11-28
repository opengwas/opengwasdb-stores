"""`pixi run yardstick`: take the workload's measures and write a result (#390).

Each Store runs in its own fresh process (`yardstick.worker`), and its peak RSS
in another, so one Store's caches and allocations never reach the next. Nothing
here writes under the Artifact Root: Stores are opened read-only, and the
fixture build and the workers' spec files go to a scratch directory under
`$YARDSTICK_TMPDIR`, else `$TMPDIR`, else `/data/opengwasdb/work/yardstick-tmp`
-- never the host's `/tmp`, because everything this project writes goes under
`/data/opengwasdb` (CONTRIBUTING, Worktrees).

A workload's `ld` section (#504) is measured after the GWAS Stores the same way, one
LD Store or Genotype Store at a time, each found through the LD Store Registry
(`workload.resolve_ld`), and written to the same result under `ld`.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ogstores import engine, paths
from yardstick import fixture, proc
from yardstick import result as result_spec
from yardstick import workload as workload_spec

BUDGET_S = 1800.0
#: Where a run's scratch lives when nothing else says. `/data` is where this
#: project writes (CONTRIBUTING, Worktrees): a run must never fill the host's `/tmp`.
SCRATCH_ROOT_ENV = "YARDSTICK_TMPDIR"
DEFAULT_SCRATCH_ROOT = Path("/data/opengwasdb/work/yardstick-tmp")


def scratch_root() -> Path:
    """The directory a run's scratch is made under: `$YARDSTICK_TMPDIR`, `$TMPDIR`, else `/data`."""
    for name in (SCRATCH_ROOT_ENV, "TMPDIR"):
        configured = os.environ.get(name)
        if configured and configured.strip():
            return Path(configured)
    return DEFAULT_SCRATCH_ROOT


def _now() -> float:
    return time.perf_counter()
RESULTS_DIR = workload_spec.REPO_ROOT / "docs" / "_yardstick"


def engine_identity() -> dict[str, str]:
    """The opengwasdb the workers run: they import it in-process, from this interpreter.

    Version and commit both come from that install (#253), and a commit it
    cannot prove is refused rather than recorded.
    """
    ran = engine.in_process()
    if not ran.is_provable():
        raise RuntimeError(f"cannot prove which opengwasdb commit this run uses (got {ran.commit!r})")
    return {"version": ran.version, "revision": ran.commit}


def _git_output(*args: str) -> str:
    """Run git in this checkout, refusing to guess when it fails."""
    completed = subprocess.run(
        ["git", *args], cwd=workload_spec.REPO_ROOT, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise RuntimeError(f"`git {' '.join(args)}` failed: {completed.stderr.strip()}")
    return completed.stdout


def ogstores_identity() -> dict[str, Any]:
    """The `ogstores` this run ran: its version, the commit, and whether it was dirty.

    An editable install's version is frozen at the commit it was built from
    (#386), so the version alone can name code that did not run. The commit
    cannot, and `dirty` says whether even the commit is the whole story: a run
    on an uncommitted tree is not reproducible, and a reader must be able to see
    that rather than infer it from a local version segment.
    """
    return {
        "version": importlib.metadata.version("ogstores"),
        "commit": _git_output("rev-parse", "HEAD").strip(),
        "dirty": bool(_git_output("status", "--porcelain").strip()),
    }


WORKER_MODULE = "yardstick.worker"
INDEX = "index.sqlite"


def guarded_indexes(store_path: Path) -> tuple[str, ...]:
    """The `index.sqlite` files the release at `store_path` ships, relative to it.

    Every 0.2 release ships one at its root. From 0.3.0 the file holds only
    `completion_quality`, so an Observed-Only release ships none, and a Hybrid's outer
    release none either: its Dense Component holds it (`dense/index.sqlite`), and the
    Hybrid facade opens that one (opengwasdb ADR 0102 §3). Which of the two a release
    ships is the engine's `ships_index`, asked of the manifest's own fields. For a
    release that ships none, a missing index is the Store as built, not a reason to refuse it.
    """
    from opengwasdb.model.manifest import StoreManifest
    from opengwasdb.store.open import ships_index

    manifest = StoreManifest.from_dict(json.loads((store_path / "manifest.json").read_text(encoding="utf-8")))
    ships = [
        (relative, ships_index(manifest.primary_layout, manifest.format_version, manifest.completion_state, dense_component=nested))
        for relative, nested in ((INDEX, False), (f"dense/{INDEX}", True))
        if not nested or manifest.primary_layout.value == "hybrid"
    ]
    return tuple(relative for relative, shipped in ships if shipped)


def snapshot(store_path: Path) -> dict[str, Any]:
    """The Store directory's top-level entries, and the size and mtime of each `index.sqlite`.

    `query_store` opens a shipped `index.sqlite` read-write (opengwasdb store/open.py), so
    a missing index would be created beside the Store. The runner records the
    directory before a Store's worker starts and compares afterwards. `index` is the
    root's, `dense_index` a Hybrid's Dense Component's.
    """

    def stamp(relative: str) -> tuple[int, int] | None:
        path = store_path / relative
        stat = path.stat() if path.is_file() else None
        return (stat.st_size, stat.st_mtime_ns) if stat else None

    return {
        "entries": sorted(os.listdir(store_path)),
        "index": stamp(INDEX),
        "dense_index": stamp(f"dense/{INDEX}") if (store_path / "dense").is_dir() else None,
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """What changed beside a Store: new top-level entries, and a changed or removed index."""
    problems = [f"new top-level entry: {name}" for name in after["entries"] if name not in before["entries"]]
    if before["index"] != after["index"]:
        problems.append(f"{INDEX} changed")
    if before["dense_index"] != after["dense_index"]:
        problems.append(f"dense/{INDEX} changed")
    return problems


def _worker(
    mode: str, store_path: Path, entry: dict[str, Any], workload: dict[str, Any], scratch: Path,
    *, timeout: float | None = None,
) -> dict[str, Any]:
    if timeout is not None and timeout <= 0:
        raise RuntimeError("timeout after 0 s: the run's budget was already spent")
    spec = scratch / f"{entry['store_id']}-{mode}.json"
    spec.write_text(
        json.dumps({
            "store_path": str(store_path),
            "entry": entry,
            "repetitions": workload["repetitions"],
            "region_width_bp": workload.get("region_width_bp"),
        }),
        encoding="utf-8",
    )
    try:
        done = proc.run_group([sys.executable, "-m", WORKER_MODULE, mode, str(spec)], timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timeout after {timeout:g} s") from None
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[-500:] or f"worker exited {done.returncode}")
    try:
        record = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise RuntimeError(f"worker printed no JSON record: {done.stdout.strip()[-200:]!r}") from None
    if not isinstance(record, dict):
        raise RuntimeError(f"worker printed a {type(record).__name__}, not a record")
    return record


def _store(
    entry: dict[str, Any], store_path: Path, workload: dict[str, Any], scratch: Path, deadline: float
) -> tuple[dict[str, Any], list[str]]:
    head = {k: entry[k] for k in ("store_id", "layout", "coverage") if k in entry}
    if entry["status"] == "pending":
        return {**head, "status": "pending", "reason": entry["reason"]}, []
    wanted = entry["measures"]
    before = snapshot(store_path)
    guarded = guarded_indexes(store_path)

    def missing() -> list[str]:
        return [relative for relative in guarded if not (store_path / relative).is_file()]

    if missing():
        error = f"{', '.join(missing())} missing; refusing to let the query path create it"
        measures = {name: {"status": "unavailable", "error": error} for name in wanted}
        return {**head, "status": "measured", "measures": measures}, []
    steps = [("timed", [n for n in wanted if n != "peak_rss"])]
    if "peak_rss" in wanted:
        steps.append(("rss", ["peak_rss"]))

    def refusal(mode: str) -> str | None:
        # Before every worker: a worker never opens a Store that has lost its index.
        if missing():
            return f"{', '.join(missing())} missing before the {mode} worker; refusing to let the query path create it"
        return None

    measures, problems = _workers(store_path, entry, workload, steps, scratch, deadline, before, refusal=refusal)
    return {**head, "status": "measured", "measures": measures}, problems


def _workers(
    store_path: Path, entry: dict[str, Any], workload: dict[str, Any], steps: list[tuple[str, list[str]]],
    scratch: Path, deadline: float, before: dict[str, Any], *, refusal: Callable[[str], str | None] = lambda mode: None,
) -> tuple[dict[str, Any], list[str]]:
    """Each step's worker in turn, each a fresh process, under the guard: the one loop GWAS and LD Stores share.

    A step is a worker mode and the measures it takes; a `peak_rss` step's record is that measure.
    `refusal(mode)`, asked before each worker, stops it and the rest with its reason. A worker that
    fails makes its measures `unavailable` with the error and the run goes on. The Store directory
    is compared with `before` after each worker, and a change stops the rest; the final comparison
    runs in a `finally`, whatever a worker did, and its problems are the Store's guard violations.
    """
    measures: dict[str, Any] = {}

    def unavailable(names: list[str], error: str) -> None:
        for name in names:
            measures[name] = {"status": "unavailable", "error": error}

    problems: list[str] = []
    try:
        for position, (mode, names) in enumerate(steps):
            refused = refusal(mode)
            if refused:
                for _, rest in steps[position:]:
                    unavailable(rest, refused)
                break
            try:
                record = _worker(mode, store_path, entry, workload, scratch, timeout=deadline - _now())
                measures.update({"peak_rss": record} if names == ["peak_rss"] else record)
            except RuntimeError as error:
                unavailable(names, str(error))
            disturbed = compare(before, snapshot(store_path))
            if disturbed:
                for _, rest in steps[position + 1:]:
                    unavailable(rest, f"skipped: the Store changed beside it ({'; '.join(disturbed)})")
                break
    finally:
        problems = compare(before, snapshot(store_path))
    return measures, problems


def _ld_entry(target: dict[str, Any], section: dict[str, Any], clump: dict[str, Any]) -> dict[str, Any]:
    """What an LD worker reads: the Store's registry facts, the workload's inputs, and the clump input
    the preflight checked, with the digest the worker's own read must match."""
    return {
        **target, "store_id": target["ld_id"],
        "matrix": section["matrix"], "region": section["region"], "proxies": section["proxies"],
        "clump": {key: clump[key] for key in ("path", "analyses", "file", "sha256")}
        | {"r2": section["clump"]["r2"], "kb": section["clump"]["kb"]},
    }


def _ld_store(
    target: dict[str, Any], section: dict[str, Any], clump: dict[str, Any], scratch: Path, deadline: float
) -> tuple[dict[str, Any], list[str]]:
    """One LD Store or Genotype Store: its timed worker, then its peak-RSS worker (`_workers`).

    Nothing opens the Store before its timed worker, so its `open` is the first. That worker
    also says what the Store cannot answer (`ld.inapplicable`); a worker that never runs, its
    budget spent, says nothing, and its rows are `unavailable` naming the budget.
    """
    head = {key: target[key] for key in ("ld_id", "kind", "label", "location")}
    location = Path(target["location"])
    wanted = list(workload_spec.LD_MEASURES)
    if not location.is_dir():
        error = f"{target['ld_id']}'s location {location} does not exist"
        return {**head, "status": "measured", "measures": {n: {"status": "unavailable", "error": error} for n in wanted}}, []
    steps = [("ld", [n for n in wanted if n != "peak_rss"]), ("ld_rss", ["peak_rss"])]
    measures, problems = _workers(location, _ld_entry(target, section, clump), {"repetitions": section["repetitions"]},
                                  steps, scratch, deadline, snapshot(location))
    return {**head, "status": "measured", "measures": {name: measures[name] for name in wanted if name in measures}}, problems


def _ld_inputs(section: dict[str, Any], clump: dict[str, Any]) -> dict[str, Any]:
    """What the LD section read, so a result says it: each variant list's rule and digest, and the clump input."""
    from yardstick.ld import variants_sha256

    matrix, proxies = section["matrix"], section["proxies"]
    return {
        "matrix": {"chromosome": matrix["chromosome"], "seed": matrix["seed"], "count": len(matrix["variants"]),
                   "variants_sha256": variants_sha256(matrix["variants"])},
        "region": {"chromosome": section["region"]["chromosome"], "centre": section["region"]["centre"],
                   "widths_bp": dict(workload_spec.LD_REGIONS)},
        "proxies": {"r2": proxies["r2"], "kb": proxies["kb"], "seeds": proxies["seeds"],
                    "variants_sha256": {name: variants_sha256(proxies["variants"][name]) for name in workload_spec.LD_PROXIES}},
        "clump": {**clump, "r2": section["clump"]["r2"], "kb": section["clump"]["kb"], "score": "|z|"},
    }


def run(
    workload: dict[str, Any],
    *,
    store_paths: dict[str, Path] | None = None,
    scratch: Path | None = None,
    stores_root: Path = paths.DEFAULT_ARTIFACT_ROOT,
    workload_path: Path = workload_spec.DEFAULT_PATH,
    budget_s: float = BUDGET_S,
    ld_root: Path = workload_spec.DEFAULT_LD_ROOT,
) -> dict[str, Any]:
    section = workload.get("ld")
    own_scratch = scratch is None
    if scratch is None:
        root = scratch_root()
        root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="yardstick-", dir=root))
    else:
        scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    start_local = datetime.now().astimezone()
    start_clock = _now()
    load_before = list(os.getloadavg())
    deadline = start_clock + budget_s
    violations: list[dict[str, str]] = []
    try:
        # The LD section's Stores and its clump input are resolved, and the input hashed, before anything
        # is measured, so a workload naming an unknown ld_id, a region wider than far_bp or an input that
        # is not the pinned one is refused whole. It is part of the run, so it counts in its time and budget.
        ld_targets = workload_spec.resolve_ld(workload, ld_root) if section else []
        clump = workload_spec.clump_input(workload) if section else None
        stores = []
        for entry in workload["stores"]:
            store_path = (store_paths or {}).get(entry["store_id"]) or paths.store_dir(entry["store_id"], stores_root) / "store.opengwasdb"
            record, problems = _store(entry, store_path, workload, scratch, deadline)
            stores.append(record)
            violations += [{"store_id": entry["store_id"], "problem": p} for p in problems]
        ld_stores = []
        for target in ld_targets:
            record, problems = _ld_store(target, section, clump, scratch, deadline)
            ld_stores.append(record)
            violations += [{"store_id": target["ld_id"], "problem": p} for p in problems]
        build = None
        if workload.get("fixture_build"):
            build_dir = scratch / "fixture-build"
            build_dir.mkdir()
            build = fixture.timed_build(build_dir, timeout=deadline - _now())
        end_local = datetime.now().astimezone()
        elapsed = _now() - start_clock
        result: dict[str, Any] = {
            "schema": result_spec.SCHEMA_VERSION,
            "workload_id": workload["workload_id"],
            "workload_sha256": workload_spec.sha256(workload_path) if workload_path.is_file() else "",
            "opengwasdb": engine_identity(),
            "ogstores": ogstores_identity(),
            "host": {"name": socket.gethostname(), "cpu_count": os.cpu_count() or 1},
            "load_average": {"before": load_before, "after": list(os.getloadavg())},
            "started_utc": start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ended_utc": end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round(elapsed, 1),
            "over_budget": elapsed > budget_s,
            "local_utc_offset_minutes": int(start_local.utcoffset().total_seconds() // 60),
            # The Veeam snapshot no longer covers /data/opengwasdb (2026-11-02, #411), so a run is
            # never in that window. The field stays because schema 1 requires it: a result measured
            # before that date keeps the value that was true of it, and the site reads it as history.
            "veeam_window": False,
            "repetitions": workload["repetitions"],
            "stores": stores,
            "guard_violations": violations,
        }
        if build is not None:
            result["fixture_build"] = build
        if section:
            result["ld"] = {
                "repetitions": section["repetitions"],
                "ogquery": {"version": importlib.metadata.version("ogquery")},
                "inputs": _ld_inputs(section, clump),
                "stores": ld_stores,
            }
        return result
    finally:
        if own_scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the yardstick and write docs/_yardstick/<UTC time>-<opengwasdb version>.json")
    parser.add_argument("--workload", type=Path, default=workload_spec.DEFAULT_PATH)
    parser.add_argument("--stores-root", type=Path, default=paths.DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)
    workload = workload_spec.load(args.workload)
    result = run(workload, stores_root=args.stores_root, workload_path=args.workload)
    result_spec.validate(result, workload)
    stamp = datetime.strptime(result["started_utc"], "%Y-%m-%dT%H:%M:%SZ").strftime("%Y%m%dT%H%M%SZ")
    out = args.output_dir / f"{stamp}-{result['opengwasdb']['version']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(out)
    if result["guard_violations"]:
        print(f"error: the run wrote beside a Store: {result['guard_violations']}", file=sys.stderr)
        return 3
    if result.get("over_budget"):
        print(f"warning: the run took {result['duration_s']} s, over the {BUDGET_S:.0f} s budget", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
