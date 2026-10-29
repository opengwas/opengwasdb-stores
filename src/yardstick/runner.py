"""`pixi run yardstick`: take the workload's measures and write a result (#390).

Each Store runs in its own fresh process (`yardstick.worker`), and its peak RSS
in another, so one Store's caches and allocations never reach the next. Nothing
here writes under the Artifact Root: Stores are opened read-only, and the
fixture build and the workers' spec files go to a scratch directory under
$TMPDIR.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ogstores import paths
from yardstick import fixture, veeam
from yardstick import result as result_spec
from yardstick import workload as workload_spec

BUDGET_S = 600.0
RESULTS_DIR = workload_spec.REPO_ROOT / "docs" / "_yardstick"


def pinned_revision(lock: Path | None = None) -> str:
    """The opengwasdb revision this checkout's pixi.lock pins."""
    text = (lock or workload_spec.REPO_ROOT / "pixi.lock").read_text(encoding="utf-8")
    found = set(re.findall(r"opengwas/opengwasdb\?rev=([0-9a-f]{40})", text))
    if len(found) != 1:
        raise RuntimeError(f"pixi.lock pins {len(found)} opengwasdb revisions: {sorted(found)}")
    return found.pop()


WORKER_MODULE = "yardstick.worker"
INDEX = "index.sqlite"


def snapshot(store_path: Path) -> dict[str, Any]:
    """The Store directory's top-level entries, and `index.sqlite`'s size and mtime.

    `query_store` opens `index.sqlite` read-write (opengwasdb store/open.py), so
    a missing index would be created beside the Store. The runner records the
    directory before a Store's worker starts and compares afterwards.
    """
    index = store_path / INDEX
    stat = index.stat() if index.is_file() else None
    return {
        "entries": sorted(os.listdir(store_path)),
        "index": (stat.st_size, stat.st_mtime_ns) if stat else None,
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """What changed beside a Store: new top-level entries, and a changed or removed index."""
    problems = [f"new top-level entry: {name}" for name in after["entries"] if name not in before["entries"]]
    if before["index"] != after["index"]:
        problems.append(f"{INDEX} changed")
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
        done = subprocess.run(
            [sys.executable, "-m", WORKER_MODULE, mode, str(spec)], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timeout after {timeout:g} s") from None
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[-500:] or f"worker exited {done.returncode}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def _store(
    entry: dict[str, Any], store_path: Path, workload: dict[str, Any], scratch: Path, deadline: float
) -> tuple[dict[str, Any], list[str]]:
    head = {k: entry[k] for k in ("store_id", "layout", "coverage") if k in entry}
    if entry["status"] == "pending":
        return {**head, "status": "pending", "reason": entry["reason"]}, []
    wanted = entry["measures"]
    before = snapshot(store_path)
    if before["index"] is None:
        error = f"{INDEX} missing; refusing to let the query path create it"
        measures = {name: {"status": "unavailable", "error": error} for name in wanted}
        return {**head, "status": "measured", "measures": measures}, []
    remaining = lambda: deadline - time.perf_counter()  # noqa: E731
    measures: dict[str, Any] = {}
    try:
        measures.update(_worker("timed", store_path, entry, workload, scratch, timeout=remaining()))
    except RuntimeError as error:
        for name in wanted:
            if name != "peak_rss":
                measures[name] = {"status": "unavailable", "error": str(error)}
    if "peak_rss" in wanted:
        try:
            measures["peak_rss"] = _worker("rss", store_path, entry, workload, scratch, timeout=remaining())
        except RuntimeError as error:
            measures["peak_rss"] = {"status": "unavailable", "error": str(error)}
    problems = compare(before, snapshot(store_path))
    return {**head, "status": "measured", "measures": measures}, problems


def run(
    workload: dict[str, Any],
    *,
    store_paths: dict[str, Path] | None = None,
    scratch: Path | None = None,
    stores_root: Path = paths.DEFAULT_ARTIFACT_ROOT,
    workload_path: Path = workload_spec.DEFAULT_PATH,
    budget_s: float = BUDGET_S,
) -> dict[str, Any]:
    own_scratch = scratch is None
    scratch = Path(scratch) if scratch else Path(tempfile.mkdtemp(prefix="yardstick-", dir=os.environ.get("TMPDIR")))
    scratch.mkdir(parents=True, exist_ok=True)
    start_local = datetime.now().astimezone()
    start_clock = time.perf_counter()
    load_before = list(os.getloadavg())
    deadline = start_clock + budget_s
    violations: list[dict[str, str]] = []
    try:
        stores = []
        for entry in workload["stores"]:
            store_path = (store_paths or {}).get(entry["store_id"]) or paths.store_dir(entry["store_id"], stores_root) / "store.opengwasdb"
            record, problems = _store(entry, store_path, workload, scratch, deadline)
            stores.append(record)
            violations += [{"store_id": entry["store_id"], "problem": p} for p in problems]
        build = None
        if workload.get("fixture_build"):
            build_dir = scratch / "fixture-build"
            build_dir.mkdir()
            build = fixture.timed_build(build_dir, timeout=deadline - time.perf_counter())
        end_local = datetime.now().astimezone()
        result: dict[str, Any] = {
            "schema": result_spec.SCHEMA_VERSION,
            "workload_id": workload["workload_id"],
            "workload_sha256": workload_spec.sha256(workload_path) if workload_path.is_file() else "",
            "opengwasdb": {"version": importlib.metadata.version("opengwasdb"), "revision": pinned_revision()},
            "ogstores": {"version": importlib.metadata.version("ogstores")},
            "host": {"name": socket.gethostname(), "cpu_count": os.cpu_count() or 1},
            "load_average": {"before": load_before, "after": list(os.getloadavg())},
            "started_utc": start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ended_utc": end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round(time.perf_counter() - start_clock, 1),
            "local_utc_offset_minutes": int(start_local.utcoffset().total_seconds() // 60),
            "veeam_window": veeam.overlaps_window(start_local, end_local),
            "repetitions": workload["repetitions"],
            "stores": stores,
            "guard_violations": violations,
        }
        if build is not None:
            result["fixture_build"] = build
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
    if result["veeam_window"]:
        print("warning: the run overlapped the Veeam window (00:00-11:00 local); veeam_window is true", file=sys.stderr)
    if result["guard_violations"]:
        print(f"error: the run wrote beside a Store: {result['guard_violations']}", file=sys.stderr)
        return 3
    if result["duration_s"] > BUDGET_S:
        print(f"warning: the run took {result['duration_s']} s, over the {BUDGET_S:.0f} s budget", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
