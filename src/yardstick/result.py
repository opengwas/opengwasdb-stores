"""The yardstick result schema and its validator (docs/spec/yardstick.md).

`validate(result, workload=None)` raises `ResultError` on the first problem. The
`schema` field is the contract with future readers: a new shape is a new version,
and a reader refuses a version it does not know.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1
TIMED = ("open", "phewas_rsid", "phewas_alid", "phewas_position", "analysis_full", "analysis_hm3", "region")
STATUSES = ("measured", "unavailable", "not_applicable")
TOP_LEVEL = (
    "schema", "workload_id", "workload_sha256", "opengwasdb", "ogstores", "host", "load_average",
    "started_utc", "ended_utc", "duration_s", "local_utc_offset_minutes", "veeam_window",
    "repetitions", "stores",
)


class ResultError(ValueError):
    pass


def _number(where: str, value: Any, *, integer: bool = False) -> None:
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise ResultError(f"{where} must be a number, got {value!r}")
    if value < 0:
        raise ResultError(f"{where} must not be negative, got {value!r}")


def _need(where: str, record: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if key not in record:
            raise ResultError(f"{where} lacks {key}")


def _measure(where: str, name: str, record: dict[str, Any]) -> None:
    status = record.get("status")
    if status not in STATUSES:
        raise ResultError(f"{where}.status must be one of {STATUSES}, got {status!r}")
    if status == "unavailable":
        if not record.get("error"):
            raise ResultError(f"{where} is unavailable and gives no error")
        return
    if status == "not_applicable":
        return
    if name in TIMED:
        _need(where, record, "first_read_ms", "median_ms", "p95_ms", "reps")
        for key in ("first_read_ms", "median_ms", "p95_ms"):
            _number(f"{where}.{key}", record[key])
        _number(f"{where}.reps", record["reps"], integer=True)
        if "result_count" in record:
            _number(f"{where}.result_count", record["result_count"], integer=True)
    elif name == "bytes_on_disk":
        _need(where, record, "bytes", "source")
        _number(f"{where}.bytes", record["bytes"], integer=True)
        if record["source"] not in ("walk", "manifest"):
            raise ResultError(f"{where}.source must be walk or manifest")
    elif name == "peak_rss":
        _need(where, record, "baseline_mb", "peak_mb", "delta_mb", "result_count")
        for key in ("baseline_mb", "peak_mb", "delta_mb"):
            _number(f"{where}.{key}", record[key])
        _number(f"{where}.result_count", record["result_count"], integer=True)
    else:
        raise ResultError(f"{where}: unknown measure {name!r}")


def validate(result: dict[str, Any], workload: dict[str, Any] | None = None) -> None:
    if result.get("schema") != SCHEMA_VERSION:
        raise ResultError(f"result schema must be {SCHEMA_VERSION}, got {result.get('schema')!r}")
    _need("result", result, *TOP_LEVEL)
    if not isinstance(result["veeam_window"], bool):
        raise ResultError("veeam_window must be true or false")
    _need("opengwasdb", result["opengwasdb"], "version", "revision")
    _need("ogstores", result["ogstores"], "version")
    _need("host", result["host"], "name", "cpu_count")
    _need("load_average", result["load_average"], "before", "after")
    _number("duration_s", result["duration_s"])
    for store in result["stores"]:
        where = f"stores[{store.get('store_id')}]"
        _need(where, store, "store_id", "layout", "status")
        if store["status"] == "pending":
            _need(where, store, "reason")
        elif store["status"] == "measured":
            _need(where, store, "measures")
            for name, record in store["measures"].items():
                _measure(f"{where}.{name}", name, record)
        else:
            raise ResultError(f"{where}.status must be measured or pending")
    build = result.get("fixture_build")
    if build is not None:
        if build.get("status") == "measured":
            _number("fixture_build.seconds", build.get("seconds"))
        elif build.get("status") != "unavailable" or not build.get("error"):
            raise ResultError("fixture_build is measured, or unavailable with an error")
    if workload is not None:
        _against(result, workload)


def _against(result: dict[str, Any], workload: dict[str, Any]) -> None:
    by_id = {s["store_id"]: s for s in result["stores"]}
    for wanted in workload["stores"]:
        got = by_id.get(wanted["store_id"])
        if got is None:
            raise ResultError(f"result lacks Store {wanted['store_id']}")
        if got["status"] != wanted["status"]:
            raise ResultError(f"{wanted['store_id']}: status {got['status']}, workload says {wanted['status']}")
        for name in wanted.get("measures") or []:
            if name not in got["measures"]:
                raise ResultError(f"{wanted['store_id']} lacks measure {name}")
