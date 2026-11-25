"""An LD Store's provenance, read through `opengwasdb ld info` (ADR 0023).

The engine's command line is the one way this registry looks inside an LD
Store; `ogstores.store_info.run_engine_json` runs it and parses its JSON, with
an injectable runner, so the tests use a recorded output and never a Store.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ld_registry.record import IMPORTED_MATRIX
from ogstores.store_info import Runner, run_engine_json

#: Each provenance field of a record, and where `opengwasdb ld info` keeps it.
#: `t_partner`, `counts` and `variants_dropped` are recorded when the Store
#: records them.
PROVENANCE_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("source_kind", ("source", "kind")),
    ("source_description", ("source", "description")),
    ("source_panel", ("panel", "id")),
    ("ancestry", ("panel", "ancestry")),
    ("n", ("panel", "n")),
    ("k", ("adjustment", "k")),
    ("reference_assembly", ("reference_assembly",)),
    ("format_version", ("format_version",)),
    ("r_scale", ("encoding", "r_scale")),
    ("near_bp", ("near_bp",)),
    ("far_bp", ("far_bp",)),
    ("t_far", ("t_far",)),
    ("t_partner", ("t_partner",)),
    ("n_variants", ("n_variants",)),
    ("counts", ("counts",)),
    ("variants_dropped", ("variants_dropped",)),
    ("software", ("software",)),
    ("created_at", ("build", "created_at")),
    ("wall_seconds", ("build", "wall_seconds")),
)
OPTIONAL = frozenset({"t_partner", "counts", "variants_dropped"})


def ld_info(location: Path | str, runner: Runner | None = None) -> dict[str, Any]:
    """The provenance `opengwasdb ld info <location>` prints."""
    return run_engine_json(["opengwasdb", "ld", "info", str(location)], runner=runner)


def provenance_sha256(info: dict[str, Any]) -> str:
    """The sha256 of the provenance as `ld info` prints it, in canonical JSON (owner, 25 November 2026)."""
    canonical = json.dumps(info, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_provenance(info: dict[str, Any]) -> dict[str, Any]:
    """The record's `provenance` section, from `ld info`'s output. A missing value is refused."""
    provenance: dict[str, Any] = {}
    for field, keys in PROVENANCE_PATHS:
        value: Any = info
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            if field in OPTIONAL:
                continue
            raise ValueError(f"`opengwasdb ld info` records no {'.'.join(keys)} (the record's provenance.{field})")
        provenance[field] = value
    if provenance["source_kind"] == IMPORTED_MATRIX:
        provenance["source_description"] = _description_object(provenance["source_description"])
    return provenance


def _description_object(description: Any) -> dict[str, Any]:
    """An imported matrix's description, which `ld info` prints as JSON text, as the object it is."""
    try:
        parsed = json.loads(description) if isinstance(description, str) else None
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        raise ValueError(
            f"`opengwasdb ld info`'s source.description is not a JSON object, as an {IMPORTED_MATRIX} source's is "
            "(ogld build-inputs.md §12)"
        )
    return parsed
