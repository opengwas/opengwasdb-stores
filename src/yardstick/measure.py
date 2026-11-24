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

from yardstick.workload import LOOKUP_SHAPES, applies, lookup_inputs

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


class NotApplicable(Exception):
    """The Store's layout refuses this query, or the Store lacks what it reads: recorded `not_applicable`."""


def open_store(store_path: Path) -> Any:
    """The engine's query handle for a Store. The one place yardstick opens a Store, so tools reuse it."""
    from opengwasdb.query import query_store

    return query_store(store_path)


def rows(result: dict[str, Any]) -> int:
    """Rows of an Associations result (`z`), or of a Rho result (`rho`, a vector or a matrix)."""
    if "z" in result:
        return len(result["z"])
    return int(result["rho"].size)


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
    default = repetitions["default"]
    reps_of = lambda name: repetitions.get(name, default)  # noqa: E731
    wanted = entry["measures"]
    out: dict[str, Any] = {}

    def attempt(name: str, fn: Callable[[], dict[str, Any]]) -> None:
        if name not in wanted:
            return
        try:
            out[name] = fn()
        except NotApplicable as why:
            out[name] = {"status": "not_applicable", "reason": str(why)}
        except Exception as error:  # a measure the Store cannot answer is recorded, not hidden
            out[name] = {"status": "unavailable", "error": f"{type(error).__name__}: {error}"[:500]}

    def open_and_close() -> None:
        handle = open_store(store_path)
        handle.close()

    attempt("open", lambda: time_calls(open_and_close, reps_of("open")))
    q = open_store(store_path)
    try:
        attempt("phewas_rsid", lambda: time_calls(lambda: q.phewas(entry["rsid"]), reps_of("phewas_rsid"), count=rows))
        attempt("phewas_alid", lambda: time_calls(lambda: q.phewas(entry["alid"]), reps_of("phewas_alid"), count=rows))
        pos = entry.get("position")
        attempt(
            "phewas_position",
            lambda: time_calls(
                lambda: q.range_phewas(pos["chromosome"], pos["position"], pos["position"]),
                reps_of("phewas_position"), count=rows,
            ),
        )
        attempt("analysis_full", lambda: time_calls(lambda: q.analysis(entry["analysis_id"]), reps_of("analysis_full"), count=rows))
        _measure_facade_queries(q, entry, reps_of, attempt)
        if "analysis_hm3" in wanted:
            def hm3() -> dict[str, Any]:
                if not applies("analysis_hm3", entry):
                    raise NotApplicable("a Ragged Store has no Indexed Variant Subset")
                from opengwasdb.layouts.dense.indexed_subsets import open_indexed_subset

                record = time_calls(
                    lambda: q.analysis(entry["analysis_id"], indexed_subset="hm3"),
                    reps_of("analysis_hm3"), count=rows, first_count_key="warm_result_count",
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
                reps_of("region"), count=rows,
            ),
        )
    finally:
        q.close()
    attempt("bytes_on_disk", lambda: bytes_on_disk(store_path))
    return out


def _measure_facade_queries(q: Any, entry: dict[str, Any], reps_of: Callable[[str], int], attempt: Callable[..., None]) -> None:
    """Metadata, the 100 kb region, lookup shapes, observed-only, top hits, cis and rho: each through `attempt`."""
    attempt("analyses_table", lambda: time_calls(q.analyses_table, reps_of("analyses_table"), count=len))
    attempt("variants_table", lambda: time_calls(q.variants_table, reps_of("variants_table"), count=len))

    def resolve() -> dict[str, Any]:
        found = q.phewas(entry["rsid"])
        return time_calls(lambda: list(q.resolve(found)), reps_of("resolve"), count=len)

    attempt("resolve", resolve)
    r100 = entry.get("region_100kb")
    attempt(
        "region_100kb",
        lambda: time_calls(
            lambda: q.range_phewas(r100["chromosome"], r100["start"], r100["end"]), reps_of("region_100kb"), count=rows
        ),
    )
    lookup = entry.get("lookup")
    for name, (n_variants, n_analyses) in LOOKUP_SHAPES.items():
        attempt(
            name,
            lambda name=name, nv=n_variants, na=n_analyses: time_calls(
                lambda: q.lookup(*lookup_inputs(lookup, nv, na)), reps_of(name), count=rows
            ),
        )
    attempt(
        "analysis_full_observed_only",
        lambda: time_calls(
            lambda: q.analysis(entry["analysis_id"], observed_only=True), reps_of("analysis_full_observed_only"), count=rows
        ),
    )

    def top_hits() -> dict[str, Any]:
        hits = entry["top_hits"]
        if entry["input_rows"]["top_hits"] == 0:
            raise NotApplicable(f"the Store has no top hits at {hits['threshold']:g}")
        record = time_calls(
            lambda: q.top_hits(analysis_id=hits["analysis_id"], threshold=hits["threshold"]), reps_of("top_hits"), count=rows
        )
        # Which path answered, from the Store's build.yaml `post.top_hits`: the index, or a scan of every cell.
        return {**record, "path": hits["path"]}

    attempt("top_hits", top_hits)

    def cis() -> dict[str, Any]:
        if not applies("range_by_analysis", entry):
            raise NotApplicable("range_by_analysis is a Ragged query")
        w = entry["cis"]
        return time_calls(lambda: q.range_by_analysis(w["chromosome"], w["start"], w["end"]), reps_of("range_by_analysis"), count=rows)

    attempt("range_by_analysis", cis)

    def rho(name: str, call: Callable[[Any, dict[str, Any]], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
        def run() -> dict[str, Any]:
            if not applies(name, entry):
                raise NotApplicable("the Store has no Rho Matrix")
            return time_calls(lambda: call(q, entry["rho"]), reps_of(name), count=rows)

        return run

    attempt("rho", rho("rho", lambda q, w: q.rho(*w["ids"])))
    attempt("rho_row", rho("rho_row", lambda q, w: q.rho_row(w["analysis_id"])))
    attempt("rho_matrix", rho("rho_matrix", lambda q, w: q.rho_matrix()))


def sample_peak(read: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """RSS before and during one read. An empty read is refused, never recorded as a peak."""
    baseline = rss_mb()
    with RssSampler() as sampler:
        result = read()
    peak = max(sampler.peak_mb, rss_mb())
    n_rows = rows(result)
    if n_rows == 0:
        raise RuntimeError("the fixed query returned no rows")
    return {
        "status": "measured",
        "baseline_mb": round(baseline, 1),
        "peak_mb": round(peak, 1),
        "delta_mb": round(peak - baseline, 1),
        "result_count": n_rows,
    }


def measure_peak_rss(store_path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Open the Store and run one full Analysis extraction, sampling RSS. Run in a fresh interpreter."""
    q = open_store(store_path)
    return sample_peak(lambda: q.analysis(entry["analysis_id"]))
