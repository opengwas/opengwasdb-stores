"""The serving policy: which registered Store Releases the catalog lists (ADR 0039).

The catalog owns this policy, not the registry (ADR 0008): the registry records
facts about releases and this module decides what they mean for serving. The
rules apply in the order of `POLICY`, and each decision carries its reason, so
"why is this Store not in the catalog?" is answerable from the catalog itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import Any

from ogstores import bundle as release_bundle

from analysis_catalog.columns import ANALYSIS_COLUMNS, MANIFEST_COLUMNS, STORE_COLUMNS

LISTED = "listed"
PLACEHOLDER = "placeholder"
EXCLUDED = "excluded"
REFUSED = "refused"

SERVED_POSTURES = ("public", "controlled")
UNSERVED_STATUSES = ("candidate", "accepted", "built")
RETIRED_STATUSES = ("superseded", "withdrawn")

# Recorded in `catalog_meta` and hashed into the version.
POLICY: tuple[str, ...] = (
    "1. bundle.check() fails: refused, with the errors",
    "2. Trial Store Release: excluded",
    "3. Access Posture embargoed: excluded",
    "4. Release Status candidate, accepted or built: excluded",
    "5. Release Status superseded or withdrawn: placeholder",
    "6. Release Status validated: listed with its Access Posture (public or controlled), "
    "unless the admitted Release Manifest count differs from the Validation Record's "
    "observed.n_analyses or that count is missing: refused",
)


class RefusedRow(ValueError):
    """A value the catalog's column type cannot hold."""


@dataclass(frozen=True)
class Decision:
    store_id: str
    decision: str
    reason: str
    # Present only for a listed Store: its `stores` row and `analyses` rows.
    store: tuple | None = None
    analyses: tuple[tuple, ...] = ()
    # Whether the decision rests on the Release Manifest, which then belongs in the version.
    read_manifest: bool = False


def decide(bundle: release_bundle.Bundle, registry: Path) -> Decision:
    """Apply the serving policy to one registered Release Bundle."""
    store_id = bundle.store_id
    # `check()` first: `Bundle.trial` raises on a malformed `trial`.
    errors = release_bundle.check(bundle, registry_root=registry)
    if errors:
        return Decision(store_id, REFUSED, "bundle.check() failed: " + "; ".join(errors), read_manifest=True)
    if bundle.trial:
        return Decision(store_id, EXCLUDED, "Trial Store Release")
    posture = bundle.release.get("access_posture")
    if posture == "embargoed":
        return Decision(store_id, EXCLUDED, "Access Posture is embargoed")
    status = bundle.status
    if status in UNSERVED_STATUSES:
        return Decision(store_id, EXCLUDED, f"Release Status is {status}, not validated")
    if status in RETIRED_STATUSES:
        return Decision(
            store_id, PLACEHOLDER,
            f"Release Status is {status}: kept as placeholder rows, not listed",
            read_manifest=True,
        )
    # check() admits only the six Release Statuses, so this one is `validated`.
    if posture not in SERVED_POSTURES:
        return Decision(store_id, REFUSED, f"Access Posture is {posture}; a listed Store is public or controlled")
    return _list_or_refuse(bundle, posture)


def _list_or_refuse(bundle: release_bundle.Bundle, posture: str) -> Decision:
    # Imported here, as bundle.py does, so importing the catalog stays cheap.
    from opengwasdb.model import analyses as opengwasdb_analyses

    store_id = bundle.store_id
    observed = _mapping((bundle.validation or {}).get("observed"))
    expected = observed.get("n_analyses")
    table = opengwasdb_analyses.read_analyses(bundle.analyses_path)
    admitted, _ = release_bundle.partition_membership(table)
    if isinstance(expected, bool) or not isinstance(expected, int):
        return Decision(store_id, REFUSED, "Validation Record has no integer observed.n_analyses", read_manifest=True)
    if len(admitted) != expected:
        return Decision(
            store_id, REFUSED,
            f"Release Manifest admits {len(admitted)} Analyses but the Validation Record's "
            f"observed.n_analyses is {expected}",
            read_manifest=True,
        )
    try:
        rows = tuple(_analysis_row(bundle, row) for row in admitted)
        store = _store_row(bundle, observed)
    except RefusedRow as exc:
        return Decision(store_id, REFUSED, str(exc), read_manifest=True)
    return Decision(
        store_id, LISTED, f"Release Status is validated, Access Posture is {posture}",
        store=store, analyses=rows, read_manifest=True,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _store_row(bundle: release_bundle.Bundle, observed: Mapping[str, Any]) -> tuple:
    release, validation = bundle.release, bundle.validation or {}
    environment = _mapping(validation.get("build_environment"))
    facts: dict[str, Any] = {
        "store_id": bundle.store_id,
        "label": release.get("label"),
        "description": release.get("description"),
        "status": bundle.status,
        "superseded_by": release.get("superseded_by"),
        "access_posture": release.get("access_posture"),
        "layout": bundle.layout,
        "completion_state": bundle.completion_state,
        # opengwasdb's values, stored as reported; null until opengwasdb#294.
        "reference_assembly": observed.get("reference_assembly"),
        "association_coverage": observed.get("association_coverage"),
        "n_analyses": observed.get("n_analyses"),
        "n_variants": observed.get("n_variants"),
        "n_associations": observed.get("n_associations"),
        "store_bytes": observed.get("store_bytes"),
        "format_version": observed.get("format_version"),
        "opengwasdb_version": environment.get("opengwasdb_version"),
        "source_snapshot_id": release.get("source_snapshot_id"),
        "accepted_at": release.get("accepted_at"),
        "validated_at": validation.get("validated_at"),
        "notes": release.get("notes"),
    }
    row = []
    for column, kind in STORE_COLUMNS.items():
        value = facts[column]
        if value is not None and kind.startswith("INTEGER"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise RefusedRow(f"Validation Record: {column} {value!r} is not an integer")
        elif value is not None:
            value = str(value)
        row.append(value)
    return tuple(row)


def _analysis_row(bundle: release_bundle.Bundle, row: Mapping[str, str]) -> tuple:
    analysis_id = (row.get("analysis_id") or "").strip()
    if ":" in analysis_id:
        raise RefusedRow(f"analyses.tsv row {analysis_id!r}: analysis_id contains ':' (#319)")
    values = {
        column: _typed(analysis_id, column, kind, row.get(column))
        for column, kind in MANIFEST_COLUMNS.items()
    }
    if values["analysis_label"] is None:
        values["analysis_label"] = values["source_label"]
    values.update(
        id=f"{bundle.store_id}:{analysis_id}",
        store_id=bundle.store_id,
        analysis_id=analysis_id,
        status=bundle.status,
    )
    return tuple(values[column] for column in ANALYSIS_COLUMNS)


def _typed(analysis_id: str, column: str, kind: str, cell: str | None) -> Any:
    """A manifest cell as its column's type; empty or absent is NULL, never 0 or ''."""
    if cell is None or not cell.strip():
        return None
    text = cell.strip()
    if kind == "TEXT":
        return text
    where = f"analyses.tsv row {analysis_id!r}: {column} {text!r}"
    try:
        number = Decimal(text)
    except InvalidOperation:
        number = None
    if kind == "INTEGER":
        if number is None or not number.is_finite() or number != number.to_integral_value():
            raise RefusedRow(f"{where} is not an integer")
        return int(number)
    if number is None or not isfinite(float(number)):
        raise RefusedRow(f"{where} is not a finite number")
    return float(number)
