"""One Store's measures, taken inside a fresh process (`python -m yardstick.worker`).

Timed measures follow opengwasdb's `measure_hm3_extract`: one first read, kept
apart, then `reps` warm reads; the median and p95 are over the warm reads only.
Peak RSS follows opengwasdb's `benchmarks/_rss.py` at the pinned revision
(that module is not importable from an installed opengwasdb, so the method is
copied here): current RSS from
/proc/self/statm sampled on a background thread during the query, because a
query's buffers are freed before it returns.
"""

from __future__ import annotations

import os
import resource
import statistics
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_PAGE_MB = resource.getpagesize() / (1024.0 * 1024.0)


def rss_mb() -> float:
    with open("/proc/self/statm") as fh:
        return int(fh.read().split()[1]) * _PAGE_MB


class RssSampler:
    def __init__(self, interval: float = 0.005) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self.peak_mb = 0.0

    def __enter__(self) -> RssSampler:
        self.peak_mb = rss_mb()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak_mb = max(self.peak_mb, rss_mb())
            self._stop.wait(self._interval)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.peak_mb = max(self.peak_mb, rss_mb())


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


def time_calls(
    fn: Callable[[], Any],
    reps: int,
    *,
    count: Callable[[Any], int] | None = None,
    first_count_key: str = "first_result_count",
) -> dict[str, Any]:
    """The first call, then `reps` warm calls.

    When `count` is given, every call's rows are counted, the first read included,
    and any empty call is refused: a fixed query that finds nothing has stopped
    measuring anything, and absence must never be recorded as a timing.
    """
    samples: list[float] = []
    first_ms = 0.0
    first_n = n = 0
    for i in range(reps + 1):
        start = time.perf_counter()
        result = fn()
        elapsed = (time.perf_counter() - start) * 1000.0
        if count is not None:
            n = count(result)
            if n == 0:
                raise RuntimeError(f"the fixed query returned no rows (call {i + 1} of {reps + 1})")
        if i == 0:
            first_ms, first_n = elapsed, n
        else:
            samples.append(elapsed)
    record: dict[str, Any] = {
        "status": "measured",
        "first_read_ms": round(first_ms, 3),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_p95(samples), 3),
        "reps": reps,
    }
    if count is not None:
        record["result_count"] = n
        record[first_count_key] = first_n
    return record


def _rows(result: dict[str, Any]) -> int:
    return len(result["z"])


def bytes_on_disk(store_path: Path) -> dict[str, Any]:
    """Apparent bytes of every file under the Store, from one walk (as `du -sb`)."""
    def fail(error: OSError) -> None:
        raise error

    total = 0
    for root, _dirs, files in os.walk(store_path, onerror=fail):
        for name in files:
            total += os.lstat(os.path.join(root, name)).st_size
    return {"status": "measured", "bytes": total, "source": "walk"}


def measure_store(store_path: Path, entry: dict[str, Any], repetitions: dict[str, int], region_width_bp: int | None = None) -> dict[str, Any]:
    """Every timed measure the workload lists for this Store, plus bytes on disk."""
    from opengwasdb.query import query_store

    default = repetitions["default"]
    reps_of = lambda name: repetitions.get(name, default)  # noqa: E731
    wanted = entry["measures"]
    out: dict[str, Any] = {}

    def attempt(name: str, fn: Callable[[], dict[str, Any]]) -> None:
        if name not in wanted:
            return
        try:
            out[name] = fn()
        except Exception as error:  # a measure the Store cannot answer is recorded, not hidden
            out[name] = {"status": "unavailable", "error": f"{type(error).__name__}: {error}"[:500]}

    def open_and_close() -> None:
        handle = query_store(store_path)
        handle.close()

    attempt("open", lambda: time_calls(open_and_close, reps_of("open")))
    q = query_store(store_path)
    try:
        attempt("phewas_rsid", lambda: time_calls(lambda: q.phewas(entry["rsid"]), reps_of("phewas_rsid"), count=_rows))
        attempt("phewas_alid", lambda: time_calls(lambda: q.phewas(entry["alid"]), reps_of("phewas_alid"), count=_rows))
        pos = entry.get("position")
        attempt(
            "phewas_position",
            lambda: time_calls(
                lambda: q.range_phewas(pos["chromosome"], pos["position"], pos["position"]),
                reps_of("phewas_position"), count=_rows,
            ),
        )
        attempt("analysis_full", lambda: time_calls(lambda: q.analysis(entry["analysis_id"]), reps_of("analysis_full"), count=_rows))
        if "analysis_hm3" in wanted:
            def hm3() -> dict[str, Any]:
                from opengwasdb.layouts.dense.indexed_subsets import open_indexed_subset

                record = time_calls(
                    lambda: q.analysis(entry["analysis_id"], indexed_subset="hm3"),
                    reps_of("analysis_hm3"), count=_rows, first_count_key="warm_result_count",
                )
                subset = open_indexed_subset(store_path, "hm3")
                return {
                    "subset": "hm3",
                    "detail": "",
                    "analysis_id": entry["analysis_id"],
                    **record,
                    "subset_n_variants": int(subset.n_subset_variants),
                    "subset_n_analyses": int(subset.n_analyses),
                    "subset_requested_count": int(subset.requested_count),
                    "subset_resolved_count": int(subset.resolved_count),
                    "subset_absent_count": int(subset.absent_count),
                    "subset_reference_assembly": str(subset.reference_assembly),
                    "subset_input_sha256": str(subset.input_sha256),
                }
            attempt("analysis_hm3", hm3)
        region = entry.get("region")
        attempt(
            "region",
            lambda: time_calls(
                lambda: q.range_phewas(region["chromosome"], region["start"], region["end"]),
                reps_of("region"), count=_rows,
            ),
        )
    finally:
        q.close()
    attempt("bytes_on_disk", lambda: bytes_on_disk(store_path))
    return out


def sample_peak(read: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """RSS before and during one read. An empty read is refused, never recorded as a peak."""
    baseline = rss_mb()
    with RssSampler() as sampler:
        result = read()
    peak = max(sampler.peak_mb, rss_mb())
    rows = _rows(result)
    if rows == 0:
        raise RuntimeError("the fixed query returned no rows")
    return {
        "status": "measured",
        "baseline_mb": round(baseline, 1),
        "peak_mb": round(peak, 1),
        "delta_mb": round(peak - baseline, 1),
        "result_count": rows,
    }


def measure_peak_rss(store_path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Open the Store and run one full Analysis extraction, sampling RSS. Run in a fresh interpreter."""
    from opengwasdb.query import query_store

    q = query_store(store_path)
    return sample_peak(lambda: q.analysis(entry["analysis_id"]))
