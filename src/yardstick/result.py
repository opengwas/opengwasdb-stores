"""The yardstick result schema and its validator (docs/spec/yardstick.md).

`validate(result, workload=None)` raises `ResultError` on the first problem. The
`schema` field is the contract with future readers: a new shape is a new version,
and a reader refuses a version it does not know.
"""

from __future__ import annotations

import re
from typing import Any

SCHEMA_VERSION = 1
TIMED = ("open", "phewas_rsid", "phewas_alid", "phewas_position", "analysis_full", "analysis_hm3", "region")
STATUSES = ("measured", "unavailable", "not_applicable")
TOP_LEVEL = (
    "schema", "workload_id", "workload_sha256", "opengwasdb", "ogstores", "host", "load_average",
    "started_utc", "ended_utc", "duration_s", "local_utc_offset_minutes", "veeam_window",
    "repetitions", "stores",
)


_REVISION = re.compile(r"[0-9a-f]{40}")
_UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


# What opengwasdb's `measure_hm3_extract` reports at the pin, beyond the timings.
HM3_COUNTS = (
    "subset_n_variants", "subset_n_analyses", "subset_requested_count",
    "subset_resolved_count", "subset_absent_count",
)
HM3_FIELDS = (
    "subset", "detail", "analysis_id", *HM3_COUNTS, "subset_reference_assembly", "subset_input_sha256",
)


class ResultError(ValueError):
    pass


def _number(where: str, value: Any, *, integer: bool = False) -> None:
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise ResultError(f"{where} must be a number, got {value!r}")
    if value < 0:
        raise ResultError(f"{where} must not be negative, got {value!r}")


def _object(where: str, value: Any) -> dict[str, Any]:
    """`value` as the object the schema requires, or a `ResultError` naming where it is not.

    Every nested value is checked before it is read: a result is data from a
    run, not a value this code built, so `"stores": [null]` is a refusal with a
    path and never an `AttributeError` (#390 review, item 1).
    """
    if not isinstance(value, dict):
        raise ResultError(f"{where} must be an object, got {value!r}")
    return value


def _list(where: str, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ResultError(f"{where} must be a list, got {value!r}")
    return value


def _need(where: str, record: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if key not in record:
            raise ResultError(f"{where} lacks {key}")


def _measure(where: str, name: str, record: Any) -> None:
    record = _object(where, record)
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
        if name != "open":
            _need(where, record, "result_count", "warm_result_count" if name == "analysis_hm3" else "first_result_count")
        for key in ("result_count", "first_result_count", "warm_result_count"):
            if key in record:
                _number(f"{where}.{key}", record[key], integer=True)
        if name == "analysis_hm3":
            _need(where, record, *HM3_FIELDS)
            for key in HM3_COUNTS:
                _number(f"{where}.{key}", record[key], integer=True)
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


def validate(result: Any, workload: dict[str, Any] | None = None) -> None:
    result = _object("result", result)
    if result.get("schema") != SCHEMA_VERSION:
        raise ResultError(f"result schema must be {SCHEMA_VERSION}, got {result.get('schema')!r}")
    _need("result", result, *TOP_LEVEL)
    if not isinstance(result["veeam_window"], bool):
        raise ResultError("veeam_window must be true or false")
    opengwasdb = _object("opengwasdb", result["opengwasdb"])
    _need("opengwasdb", opengwasdb, "version", "revision")
    if not (isinstance(opengwasdb["revision"], str) and _REVISION.fullmatch(opengwasdb["revision"])):
        raise ResultError("opengwasdb.revision must be 40 lower-case hex characters")
    for key in ("started_utc", "ended_utc"):
        if not (isinstance(result[key], str) and _UTC.fullmatch(result[key])):
            raise ResultError(f"{key} must be UTC ISO, YYYY-MM-DDTHH:MM:SSZ, got {result[key]!r}")
    ogstores = _object("ogstores", result["ogstores"])
    _need("ogstores", ogstores, "version", "commit", "dirty")
    if not (isinstance(ogstores["commit"], str) and _REVISION.fullmatch(ogstores["commit"])):
        raise ResultError("ogstores.commit must be 40 lower-case hex characters")
    if not isinstance(ogstores["dirty"], bool):
        raise ResultError("ogstores.dirty must be true or false")
    _object("host", result["host"])
    _need("host", result["host"], "name", "cpu_count")
    load_average = _object("load_average", result["load_average"])
    _need("load_average", load_average, "before", "after")
    for key in ("before", "after"):
        values = load_average[key]
        if not isinstance(values, list) or len(values) != 3:
            raise ResultError(f"load_average.{key} must be the 1, 5 and 15 minute averages")
        for value in values:
            _number(f"load_average.{key}", value)
    _number("duration_s", result["duration_s"])
    if "over_budget" in result and not isinstance(result["over_budget"], bool):
        raise ResultError("over_budget must be true or false")
    for index, value in enumerate(_list("stores", result["stores"])):
        store = _object(f"stores[{index}]", value)
        where = f"stores[{store.get('store_id')}]"
        _need(where, store, "store_id", "layout", "coverage", "status")
        if store["status"] == "pending":
            _need(where, store, "reason")
        elif store["status"] == "measured":
            _need(where, store, "measures")
            measures = _object(f"{where}.measures", store["measures"])
            for name, record in measures.items():
                _measure(f"{where}.{name}", name, record)
        else:
            raise ResultError(f"{where}.status must be measured or pending")
    build = result.get("fixture_build")
    if build is not None:
        build = _object("fixture_build", build)
        if build.get("status") == "measured":
            _need("fixture_build", build, "layout", "seconds")
            _number("fixture_build.seconds", build.get("seconds"))
        elif build.get("status") != "unavailable" or not build.get("error"):
            raise ResultError("fixture_build is measured, or unavailable with an error")
    violations = result.get("guard_violations", [])
    if not isinstance(violations, list):
        raise ResultError("guard_violations must be a list")
    for violation in violations:
        if not (isinstance(violation, dict) and {"store_id", "problem"} <= violation.keys()):
            raise ResultError("guard_violations entries are {store_id, problem}")
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
