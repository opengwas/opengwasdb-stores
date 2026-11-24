"""Load and check the yardstick workload (resources/yardstick/workload.yaml)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "resources" / "yardstick" / "workload.yaml"

SCHEMA = 1
#: Lookup shapes: variants x Analyses, each drawn from the entry's fixed 100 (`lookup_inputs`: the first variant, every tenth, or all).
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


TOP_HITS_PATHS = ("index", "scan")


def applies(name: str, store: dict[str, Any]) -> bool:
    """Whether a measure can run on this Store, from its declared `layout` and `rho` alone.

    The one owner of the layout rule: a measure that does not apply is recorded `not_applicable` and needs
    no input or count. `analysis_hm3` is a Dense or Hybrid query, `range_by_analysis` a Ragged one, and
    the rho reads need a non-Ragged Store whose entry declares a Rho Matrix.
    """
    if name == "range_by_analysis":
        return store["layout"] == "ragged"
    if name == "analysis_hm3":
        return store["layout"] != "ragged"
    if name in ("rho", "rho_row", "rho_matrix"):
        return store["layout"] != "ragged" and bool(store.get("rho"))
    return True


#: Measures that read an input and so need `input_rows[<measure>]`: the rows one call returned when the
#: workload was written. `range_by_analysis` only on a Ragged entry, rho only where `rho` declares a Rho Matrix.
ROW_COUNTED = (
    "phewas_rsid", "phewas_alid", "phewas_position", "resolve", "region_100kb", "region",
    *LOOKUP_SHAPES, "analysis_full", "analysis_full_observed_only", "top_hits", "range_by_analysis",
    "rho", "rho_row", "rho_matrix",
)


def lookup_inputs(lookup: dict[str, Any], n_variants: int, n_analyses: int) -> tuple[list[str], list[str]]:
    """A lookup shape's variants and Analyses from the entry's fixed lists of 100.

    One variant is the first; ten are every tenth, so they stay scattered; 100 are all. Analyses are the first N.
    """
    variants = lookup["variants"]
    picked = variants[:1] if n_variants == 1 else variants[:: LOOKUP_N // n_variants]
    return picked, lookup["analyses"][:n_analyses]


def _check_inputs(store_id: str, store: dict[str, Any]) -> None:
    lookup = store.get("lookup")
    if lookup is not None:
        for key in ("variants", "analyses"):
            values = lookup.get(key)
            if not isinstance(values, list) or len(values) != LOOKUP_N or len(set(values)) != LOOKUP_N:
                raise WorkloadError(f"{store_id}: lookup.{key} must be {LOOKUP_N} distinct identifiers")
    hits = store.get("top_hits")
    if hits is not None and hits.get("path") not in TOP_HITS_PATHS:
        raise WorkloadError(f"{store_id}: top_hits.path must be one of {TOP_HITS_PATHS}, got {hits.get('path')!r}")
    counts = store.get("input_rows") or {}
    for name in store["measures"]:
        if name not in ROW_COUNTED or not applies(name, store):
            continue
        rows = counts.get(name)
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise WorkloadError(f"{store_id}: input_rows lacks a row count for {name}")
        # `top_hits` may be empty by design: the Store has none, and the measure is `not_applicable`.
        if rows == 0 and name != "top_hits":
            raise WorkloadError(f"{store_id}: input for {name} returned no rows when it was checked")


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
                if name in ("rho", "rho_row", "rho_matrix") and "rho" not in store:
                    raise WorkloadError(f"{store_id}: measure {name} needs 'rho': false, or the Rho Matrix's ids")
                if not applies(name, store):
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
