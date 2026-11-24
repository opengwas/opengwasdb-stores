"""Load and check the yardstick workload (resources/yardstick/workload.yaml)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "resources" / "yardstick" / "workload.yaml"

SCHEMA = 1
#: Lookup shapes: variants x Analyses, each the first N of the entry's fixed 100.
LOOKUP_N = 100
LOOKUP_SHAPES = {
    "lookup_1x100": (1, 100), "lookup_100x1": (100, 1), "lookup_10x100": (10, 100),
    "lookup_100x10": (100, 10), "lookup_100x100": (100, 100),
}
MEASURES = (
    "open", "analyses_table", "variants_table", "resolve",
    "phewas_rsid", "phewas_alid", "phewas_position",
    "region_100kb", "region", *LOOKUP_SHAPES,
    "analysis_full", "analysis_full_observed_only", "analysis_hm3",
    "top_hits", "range_by_analysis", "rho", "rho_row", "rho_matrix",
    "bytes_on_disk", "peak_rss",
)
LAYOUTS = ("dense", "ragged", "hybrid")
# What each measure reads from a Store's entry in the workload.
NEEDS = {
    "resolve": ("rsid",),
    "region_100kb": ("region_100kb",),
    "analysis_full_observed_only": ("analysis_id",),
    "top_hits": ("top_hits",),
    "range_by_analysis": ("cis",),
    "rho": ("rho",), "rho_row": ("rho",), "rho_matrix": ("rho",),
    **{name: ("lookup",) for name in LOOKUP_SHAPES},
    "phewas_rsid": ("rsid",),
    "phewas_alid": ("alid",),
    "phewas_position": ("position",),
    "analysis_full": ("analysis_id",),
    "analysis_hm3": ("analysis_id",),
    "peak_rss": ("analysis_id",),
    "region": ("region",),
}


class WorkloadError(ValueError):
    pass


#: A measure only some layouts answer needs its input only on that layout; elsewhere it is `not_applicable`.
ONLY_ON_LAYOUT = {"range_by_analysis": "ragged"}
#: Inputs that may return no rows by design, and so record `not_applicable`.
MAY_BE_EMPTY = ("top_hits",)


def _check_inputs(store_id: str, store: dict[str, Any]) -> None:
    lookup = store.get("lookup")
    if lookup is not None:
        for key in ("variants", "analyses"):
            values = lookup.get(key)
            if not isinstance(values, list) or len(values) != LOOKUP_N or len(set(values)) != LOOKUP_N:
                raise WorkloadError(f"{store_id}: lookup.{key} must be {LOOKUP_N} distinct identifiers")
    # One read-only call per input was made when the workload was written; an input that returned nothing is wrong.
    for name, rows in (store.get("input_rows") or {}).items():
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise WorkloadError(f"{store_id}: input_rows.{name} must be a count")
        if rows == 0 and name not in MAY_BE_EMPTY:
            raise WorkloadError(f"{store_id}: input {name} returned no rows when it was checked")


def check(workload: dict[str, Any]) -> None:
    if workload.get("schema") != SCHEMA:
        raise WorkloadError(f"workload schema must be {SCHEMA}, got {workload.get('schema')!r}")
    stores = workload.get("stores")
    if not isinstance(stores, list) or not stores:
        raise WorkloadError("workload lists no stores")
    seen: set[str] = set()
    for store in stores:
        store_id = store.get("store_id")
        if not store_id or store_id in seen:
            raise WorkloadError(f"missing or duplicate store_id {store_id!r}")
        seen.add(store_id)
        if store.get("layout") not in LAYOUTS:
            raise WorkloadError(f"{store_id}: layout must be one of {LAYOUTS}")
        status = store.get("status")
        if status == "pending":
            if store.get("measures"):
                raise WorkloadError(f"{store_id}: a pending Store lists no measures")
            if not store.get("reason"):
                raise WorkloadError(f"{store_id}: a pending Store says why")
        elif status == "measured":
            measures = store.get("measures") or []
            for name in measures:
                if name not in MEASURES:
                    raise WorkloadError(f"{store_id}: unknown measure {name!r}")
                if ONLY_ON_LAYOUT.get(name, store["layout"]) != store["layout"]:
                    continue
                for key in NEEDS.get(name, ()):
                    if key not in store:
                        raise WorkloadError(f"{store_id}: measure {name} needs {key!r}")
            _check_inputs(store_id, store)
        else:
            raise WorkloadError(f"{store_id}: status must be measured or pending")


def load(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    workload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    check(workload)
    return workload


def sha256(path: Path = DEFAULT_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
