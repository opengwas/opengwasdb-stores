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
    "5. Release Status superseded or withdrawn: placeholder rows, after the count and type checks of rule 6",
    "6. Release Status validated: listed with its Access Posture (public or controlled), "
    "unless the admitted Release Manifest count differs from the Validation Record's "
    "observed.n_analyses or that count is missing: refused",
)


# SQLite INTEGER is a signed 64-bit value; a larger one fails at insert.
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


# The schema's own absence marker (docs/release-metadata-schema.md:303, :307):
# a literal `NaN` in these two columns means "unavailable" and "no MAF filter",
# distinct from `0`, which means filtering was explicitly disabled. So it is
# NULL here. In any other numeric column `NaN` is still refused.
NAN_IS_ABSENT = frozenset({"info_score_threshold", "maf_threshold"})


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


def decide(bundle: release_bundle.Bundle, registry: Path) -> Decision:
    """Apply the serving policy to one registered Release Bundle."""
    store_id = bundle.store_id
    # `check()` first: `Bundle.trial` raises on a malformed `trial`.
    errors = release_bundle.check(bundle, registry_root=registry)
    if errors:
        return Decision(store_id, REFUSED, "bundle.check() failed: " + "; ".join(errors))
    if bundle.trial:
        return Decision(store_id, EXCLUDED, "Trial Store Release")
    posture = bundle.release.get("access_posture")
    if posture == "embargoed":
        return Decision(store_id, EXCLUDED, "Access Posture is embargoed")
    status = bundle.status
    if status in UNSERVED_STATUSES:
        return Decision(store_id, EXCLUDED, f"Release Status is {status}, not validated")
    if status in RETIRED_STATUSES:
        return _admit(
            bundle, PLACEHOLDER, f"Release Status is {status}: kept as placeholder rows, not listed"
        )
    # check() admits only the six Release Statuses, so this one is `validated`.
    if posture not in SERVED_POSTURES:
        return Decision(store_id, REFUSED, f"Access Posture is {posture}; a listed Store is public or controlled")
    return _admit(bundle, LISTED, f"Release Status is validated, Access Posture is {posture}")


def _admit(bundle: release_bundle.Bundle, decision: str, reason: str) -> Decision:
    """Read a Store's rows as `decision` (listed or placeholder), or refuse it.

    A placeholder passes the same count and type checks as a listed Store.
    """
    # Imported here, as bundle.py does, so importing the catalog stays cheap.
    from opengwasdb.model import analyses as opengwasdb_analyses

    store_id = bundle.store_id
    observed = _mapping((bundle.validation or {}).get("observed"))
    expected = observed.get("n_analyses")
    table = opengwasdb_analyses.read_analyses(bundle.analyses_path)
    admitted, _ = release_bundle.partition_membership(table)
    if isinstance(expected, bool) or not isinstance(expected, int):
        return Decision(store_id, REFUSED, "Validation Record has no integer observed.n_analyses")
    if len(admitted) != expected:
        return Decision(
            store_id, REFUSED,
            f"Release Manifest admits {len(admitted)} Analyses but the Validation Record's "
            f"observed.n_analyses is {expected}",
        )
    try:
        rows = tuple(_analysis_row(bundle, row, decision == LISTED) for row in admitted)
        store = _store_row(bundle, observed)
    except RefusedRow as exc:
        return Decision(store_id, REFUSED, str(exc))
    return Decision(store_id, decision, reason, store=store, analyses=rows)


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
        # A withdrawn Store has no successor, whatever the file says.
        "superseded_by": None if bundle.status == "withdrawn" else release.get("superseded_by"),
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
            if isinstance(value, bool) or not isinstance(value, int) or not INT64_MIN <= value <= INT64_MAX:
                raise RefusedRow(f"Validation Record: {column} {value!r} is not an integer SQLite can store")
        elif value is not None:
            value = str(value)
        row.append(value)
    return tuple(row)


def outcome_type(stored_effect_scale: str | None, sample_size_kind: str | None) -> str | None:
    """`binary`, `continuous` or None.

    Binary when `sample_size_kind` is `case_control` or `stored_effect_scale` is
    `log_or` (the owner's ruling on #324): an `sd` effect with case counts is a
    binary trait stored in SD units. Continuous for `sd` with `total`. None for
    every other pair, including a blank, rather than a guess.
    """
    if sample_size_kind == "case_control" or stored_effect_scale == "log_or":
        return "binary"
    if stored_effect_scale == "sd" and sample_size_kind == "total":
        return "continuous"
    return None


def ancestry_category(assigned_ancestry: str | None, source_ancestry_label: str | None) -> str | None:
    """The catalog's ancestry dimension: Assigned Ancestry when set, `META` when it is
    empty and the Source Ancestry Label is exactly `META`, None (unassigned) otherwise."""
    if assigned_ancestry is not None:
        return assigned_ancestry
    return "META" if source_ancestry_label == "META" else None


def trait_class(context: str | None, tissue: str | None, trait_chr: str | None) -> str:
    """`molecular` when any of the three is set, `phenotype` otherwise."""
    return "molecular" if any(value is not None for value in (context, tissue, trait_chr)) else "phenotype"


def _analysis_row(bundle: release_bundle.Bundle, row: Mapping[str, str], listed: bool) -> tuple:
    analysis_id = (row.get("analysis_id") or "").strip()
    if ":" in analysis_id:
        raise RefusedRow(f"analyses.tsv row {analysis_id!r}: analysis_id contains ':' (#319)")
    values = {
        column: _typed(analysis_id, column, kind, row.get(column))
        for column, kind in MANIFEST_COLUMNS.items()
    }
    from_source = values["analysis_label"] is None and values["source_label"] is not None
    if from_source:
        values["analysis_label"] = values["source_label"]
    values.update(
        id=f"{bundle.store_id}:{analysis_id}",
        store_id=bundle.store_id,
        analysis_id=analysis_id,
        status=bundle.status,
        listed=int(listed),
        label_from_source=int(from_source),
        outcome_type=outcome_type(values["stored_effect_scale"], values["sample_size_kind"]),
        ancestry_category=ancestry_category(values["assigned_ancestry"], values["source_ancestry_label"]),
        trait_class=trait_class(values["context"], values["tissue"], values["trait_chr"]),
    )
    return tuple(values[column] for column in ANALYSIS_COLUMNS)


def _typed(analysis_id: str, column: str, kind: str, cell: str | None) -> Any:
    """A manifest cell as its column's type; empty or absent is NULL, never 0 or ''."""
    if cell is None or not cell.strip():
        return None
    text = cell.strip()
    if kind == "TEXT":
        return text
    if text == "NaN" and column in NAN_IS_ABSENT:
        return None
    where = f"analyses.tsv row {analysis_id!r}: {column} {text!r}"
    try:
        number = Decimal(text)
    except InvalidOperation:
        number = None
    # Finiteness first: comparing or converting a NaN, sNaN or infinity raises.
    if kind == "INTEGER":
        if number is None or not number.is_finite() or number != number.to_integral_value():
            raise RefusedRow(f"{where} is not an integer")
        value = int(number)
        if not INT64_MIN <= value <= INT64_MAX:
            raise RefusedRow(f"{where} is not an integer SQLite can store")
        return value
    if number is None or not number.is_finite():
        raise RefusedRow(f"{where} is not a finite number")
    value = float(number)
    # A finite, nonzero value must not become 0.0 or inf: that is a wrong value, not a rounded one.
    if not isfinite(value) or (value == 0.0 and number != 0):
        raise RefusedRow(f"{where} is not representable as a REAL")
    return value
