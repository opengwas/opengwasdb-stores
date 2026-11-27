"""The yardstick result schema and its validator (docs/spec/yardstick.md).

`validate(result, workload=None)` raises `ResultError` on the first problem. The
`schema` field is the contract with future readers: a new shape is a new version,
and a reader refuses a version it does not know.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from typing import Any

from ld_registry.ids import is_valid_ld_id
from ld_registry.record import KINDS
from yardstick.ld_queries import LD_PROXIES, LD_REGIONS
from yardstick.ld_queries import LD_TIMED as LD_TIMED

SCHEMA_VERSION = 1
TIMED = (
    "open", "analyses_table", "variants_table", "resolve",
    "phewas_rsid", "phewas_alid", "phewas_position", "region_100kb", "region",
    "lookup_1x100", "lookup_100x1", "lookup_10x100", "lookup_100x10", "lookup_100x100",
    "analysis_full", "analysis_full_observed_only", "analysis_hm3",
    "top_hits", "range_by_analysis", "rho", "rho_row", "rho_matrix",
)
# The LD section's timed queries (#504), `LD_TIMED`, come from `yardstick.ld_queries`, the table the
# workload's shapes come from too. They are kept apart from `TIMED`: og-dev-hub draws every `TIMED` name
# as a GWAS Store measure, and these are measured on LD Stores and Genotype Stores (`result["ld"]`).
#: Every LD Store is measured on all of these: the timed queries, its size and its peak memory.
LD_MEASURES = (*LD_TIMED, "bytes_on_disk", "peak_rss")
#: The kinds the LD section measures: the LD Store Registry's own (docs/spec/ld-store-registry.md).
LD_KINDS = KINDS
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
    if not math.isfinite(value):
        raise ResultError(f"{where} must be finite, got {value!r}")
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


def _measure(where: str, name: str, record: Any, timed: tuple[str, ...] = TIMED) -> None:
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
    if name in timed:
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
    if "ld" in result:
        _ld(result["ld"])
    if workload is not None:
        _against(result, workload)


#: Whole-number fields an LD record may carry beyond the GWAS ones: sizes and counts.
LD_COUNTS = ("store_bytes", "genotype_bytes", "absent", "analyses", "hits", "leads", "min_leads")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256(where: str, value: Any) -> None:
    if not (isinstance(value, str) and _SHA256.fullmatch(value)):
        raise ResultError(f"{where} must be 64 lower-case hex characters, got {value!r}")


def _whole(where: str, value: Any) -> None:
    """A whole number of either sign, such as a seed."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultError(f"{where} must be a whole number, got {value!r}")


def _text(where: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ResultError(f"{where} must be text, got {value!r}")


def _ld_inputs(inputs: Any) -> None:
    """What the run read, as the schema names it: each list's rule and digest, the regions, the clump input."""
    inputs = _object("ld.inputs", inputs)
    _need("ld.inputs", inputs, "matrix", "region", "proxies", "clump")
    matrix = _object("ld.inputs.matrix", inputs["matrix"])
    _need("ld.inputs.matrix", matrix, "chromosome", "seed", "count", "variants_sha256")
    _text("ld.inputs.matrix.chromosome", matrix["chromosome"])
    _whole("ld.inputs.matrix.seed", matrix["seed"])
    _number("ld.inputs.matrix.count", matrix["count"], integer=True)
    _sha256("ld.inputs.matrix.variants_sha256", matrix["variants_sha256"])
    region = _object("ld.inputs.region", inputs["region"])
    _need("ld.inputs.region", region, "chromosome", "centre", "widths_bp")
    _text("ld.inputs.region.chromosome", region["chromosome"])
    _number("ld.inputs.region.centre", region["centre"], integer=True)
    widths = _object("ld.inputs.region.widths_bp", region["widths_bp"])
    if sorted(widths) != sorted(LD_REGIONS):
        raise ResultError(f"ld.inputs.region.widths_bp must name {sorted(LD_REGIONS)}, got {sorted(map(str, widths))}")
    for name, width in widths.items():
        _number(f"ld.inputs.region.widths_bp.{name}", width, integer=True)
    proxies = _object("ld.inputs.proxies", inputs["proxies"])
    _need("ld.inputs.proxies", proxies, "r2", "kb", "seeds", "variants_sha256")
    _number("ld.inputs.proxies.r2", proxies["r2"])
    _number("ld.inputs.proxies.kb", proxies["kb"])
    seeds = _object("ld.inputs.proxies.seeds", proxies["seeds"])
    digests = _object("ld.inputs.proxies.variants_sha256", proxies["variants_sha256"])
    for name in LD_PROXIES:
        _need("ld.inputs.proxies.seeds", seeds, name)
        _whole(f"ld.inputs.proxies.seeds.{name}", seeds[name])
        _need("ld.inputs.proxies.variants_sha256", digests, name)
        _sha256(f"ld.inputs.proxies.variants_sha256.{name}", digests[name])
    clump = _object("ld.inputs.clump", inputs["clump"])
    _need("ld.inputs.clump", clump, "path", "analyses", "hits", "manifest_sha256", "sha256")
    _text("ld.inputs.clump.path", clump["path"])
    _sha256("ld.inputs.clump.manifest_sha256", clump["manifest_sha256"])
    _sha256("ld.inputs.clump.sha256", clump["sha256"])
    _list("ld.inputs.clump.analyses", clump["analyses"])
    _number("ld.inputs.clump.hits", clump["hits"], integer=True)


def _ld(section: Any) -> None:
    """The LD section (#504): optional, so every result written before it stays valid.

    Stricter than a GWAS Store's records, which keep their committed shape: the section names
    the reader that ran (`ogquery.version`) and what it read (`_ld_inputs`), each Store its
    registry id, every numeric field of a measured record is non-negative and every size and
    count a whole number, and a `not_applicable` gives its reason. A value of the wrong shape
    is a refusal naming its path, never a crash.
    """
    section = _object("ld", section)
    _need("ld", section, "repetitions", "ogquery", "inputs", "stores")
    _object("ld.repetitions", section["repetitions"])
    ogquery = _object("ld.ogquery", section["ogquery"])
    _need("ld.ogquery", ogquery, "version")
    _text("ld.ogquery.version", ogquery["version"])
    _ld_inputs(section["inputs"])
    seen: set[str] = set()
    for index, value in enumerate(_list("ld.stores", section["stores"])):
        store = _object(f"ld.stores[{index}]", value)
        if not is_valid_ld_id(store.get("ld_id")):
            raise ResultError(f"ld.stores[{index}].ld_id must be an LD Store Registry id, got {store.get('ld_id')!r}")
        if store["ld_id"] in seen:  # og-dev-hub keys rows by ld_id: a second would hide the first
            raise ResultError(f"ld.stores[{index}]: duplicate ld_id {store['ld_id']}")
        seen.add(store["ld_id"])
        where = f"ld.stores[{store['ld_id']}]"
        _need(where, store, "kind", "label", "location", "status", "measures")
        _text(f"{where}.label", store["label"])
        _text(f"{where}.location", store["location"])
        if store["kind"] not in LD_KINDS:
            raise ResultError(f"{where}.kind must be one of {LD_KINDS}, got {store['kind']!r}")
        if store["status"] != "measured":
            raise ResultError(f"{where}.status must be measured, got {store['status']!r}")
        for name, record in _object(f"{where}.measures", store["measures"]).items():
            if name not in LD_MEASURES:
                raise ResultError(f"{where}: unknown measure {name!r}")
            _measure(f"{where}.{name}", name, record, LD_TIMED)
            if record["status"] == "not_applicable" and not record.get("reason"):
                raise ResultError(f"{where}.{name} is not_applicable and gives no reason")
            if name == "clump" and record["status"] == "measured":
                _need(f"{where}.clump", record, "analyses", "hits", "leads")
            for key in LD_COUNTS:
                if key in record:
                    _number(f"{where}.{name}.{key}", record[key], integer=True)
            for key, field in record.items():
                if isinstance(field, (int, float)) and not isinstance(field, bool):
                    _number(f"{where}.{name}.{key}", field)


def _complete(
    noun: str, key: str, got: Iterable[dict[str, Any]], wanted: Iterable[dict[str, Any]],
    fields: tuple[str, ...], measures_of: Callable[[dict[str, Any]], Iterable[str]],
) -> None:
    """Every Store the workload names is in the result, agreeing on `fields`, with every measure it lists."""
    for store in got:
        if not isinstance(store[key], str):
            raise ResultError(f"a {noun}'s {key} must be text, got {store[key]!r}")
    by_id = {s[key]: s for s in got}
    for entry in wanted:
        found = by_id.get(entry[key])
        if found is None:
            raise ResultError(f"result lacks {noun} {entry[key]}")
        for field in fields:
            if found[field] != entry[field]:
                raise ResultError(f"{entry[key]}: {field} {found[field]}, workload says {entry[field]}")
        for name in measures_of(entry):
            if name not in found["measures"]:
                raise ResultError(f"{entry[key]} lacks measure {name}")


def _against(result: dict[str, Any], workload: dict[str, Any]) -> None:
    _complete("Store", "store_id", result["stores"], workload["stores"], ("status",),
              lambda entry: entry.get("measures") or [])
    wanted_ld = (workload.get("ld") or {}).get("stores") or []
    if not wanted_ld:
        return
    if "ld" not in result:
        raise ResultError("result lacks its ld section")
    _complete("LD Store", "ld_id", result["ld"]["stores"], wanted_ld, ("kind",), lambda entry: LD_MEASURES)
