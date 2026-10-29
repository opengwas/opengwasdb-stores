"""Load and check the yardstick workload (resources/yardstick/workload.yaml)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "resources" / "yardstick" / "workload.yaml"

SCHEMA = 1
MEASURES = (
    "open", "phewas_rsid", "phewas_alid", "phewas_position",
    "analysis_full", "analysis_hm3", "region", "bytes_on_disk", "peak_rss",
)
LAYOUTS = ("dense", "ragged", "hybrid")
# What each measure reads from a Store's entry in the workload.
NEEDS = {
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
                for key in NEEDS.get(name, ()):
                    if key not in store:
                        raise WorkloadError(f"{store_id}: measure {name} needs {key!r}")
        else:
            raise WorkloadError(f"{store_id}: status must be measured or pending")


def load(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    workload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    check(workload)
    return workload


def sha256(path: Path = DEFAULT_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
