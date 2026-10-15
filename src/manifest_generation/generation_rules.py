"""Manifest Generation's own rules for a candidate's analyses.tsv (#250).

The Release Bundle contract (`ogstores.bundle.check`) holds what both workflows
share and what Store Release Build needs to consume a bundle. These rules are
Manifest Generation's policy instead: which columns a generator must resolve,
and how it records the INFO and MAF filters it requests. The emitter runs them
before a candidate lands, and `pixi run bundle-check` runs them over every
committed bundle beside the contract, so nothing escapes either. Both call
`check_with_contract()`, as does any generator that stages its own bundles.

`check()` accumulates every error and never raises, like the contract.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from ogstores.bundle import Bundle, CheckResult
from ogstores.bundle import check as check_contract

#: The columns a Manifest Generator resolves for every Analysis.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "assigned_ancestry",
    "ancestry_assignment_method",
    "stored_effect_scale",
    "original_sd_method",
)

# The optional per-Analysis INFO policy columns (stores #175). A requested
# floor (`info_score_threshold`) and an imputation score declaration
# (`imputation_score_column`, `imputation_score_kind`,
# `imputation_score_provenance`) are one policy: a numeric threshold without the
# complete triple, or any triple cell without a numeric threshold, is
# incoherent and rejected. Older bundles may omit all of them.
INFO_SCORE_COLUMNS: tuple[str, ...] = (
    "info_score_threshold",
    "imputation_score_column",
    "imputation_score_kind",
    "imputation_score_provenance",
)
INFO_SCORE_KINDS: frozenset[str] = frozenset({"imputation_info", "imputation_r2"})

# The optional per-Analysis MAF floor column (stores #176). A numeric value is
# the requested floor in [0, 0.5] (0 disables); literal NaN means no validated
# MAF evidence -- no configured default, an exempt genotyping technology, a
# legacy record, or unusable evidence. The pinned OpenGWASDB Analysis schema may
# predate this column, so it is validated here independently rather than being
# left to the core reader.
MAF_THRESHOLD_COLUMN: str = "maf_threshold"


def check_with_contract(bundle: Bundle, *, registry_root: Path) -> CheckResult:
    """The contract's errors and these rules', with the contract's tolerated gaps.

    A generated bundle is held to both at once, so no caller can run one and
    forget the other.
    """
    contract = check_contract(bundle, registry_root=registry_root)
    return CheckResult([*contract, *check(bundle)], contract.tolerated)


def check(bundle: Bundle) -> CheckResult:
    """Every Manifest Generation rule `bundle`'s analyses.tsv breaks."""
    # Imported here, as `ogstores.bundle` does, so importing this module does
    # not cost the release path opengwasdb's model.
    from opengwasdb.model import analyses as opengwasdb_analyses

    try:
        analyses_path = Path(bundle.analyses_path)
        if not analyses_path.is_file() or analyses_path.is_symlink():
            return CheckResult([f"analyses file is missing or a symbolic link: {analyses_path}"])
        table = opengwasdb_analyses.read_analyses(analyses_path)
    except Exception as exc:  # Diagnostic, never exceptional, like the contract.
        return CheckResult([f"failed to read analyses.tsv: {type(exc).__name__}: {exc}"])

    errors = [
        f"analyses.tsv is missing Manifest Generation required column: {column!r}"
        for column in REQUIRED_COLUMNS
        if column not in table.fieldnames
    ]
    if any(column in table.fieldnames for column in INFO_SCORE_COLUMNS):
        for row in table.rows:
            errors += _info_score_errors(row)
    if MAF_THRESHOLD_COLUMN in table.fieldnames:
        for row in table.rows:
            errors += _maf_threshold_errors(row)
    return CheckResult(errors)


def _info_score_errors(row: dict[str, str]) -> list[str]:
    """A literal NaN means no validated score evidence; zero is the explicit disabled filter.

    The threshold is a request, not a fact: it is coherent only alongside the
    complete per-Analysis declaration (`imputation_score_column`, kind and
    provenance), so a numeric threshold with a partial triple and a triple with
    no numeric threshold are both rejected (stores #175).
    """
    errors: list[str] = []
    analysis_id = row.get("analysis_id") or "<unknown analysis_id>"
    value = (row.get("info_score_threshold") or "").strip()
    declaration = {column: (row.get(column) or "").strip() for column in INFO_SCORE_COLUMNS[1:]}
    numeric = False
    if value and value != "NaN":
        try:
            number = Decimal(value)
        except InvalidOperation:
            number = Decimal("NaN")
        numeric = number.is_finite() and 0 <= number <= 1
    if not numeric and value != "NaN":
        errors.append(
            f"analysis {analysis_id!r} has info_score_threshold {value!r}; "
            "expected literal NaN or a number in [0,1]"
        )
    complete = all(declaration.values())
    if numeric and not complete:
        missing = [column for column, cell in declaration.items() if not cell]
        errors.append(
            f"analysis {analysis_id!r} has a numeric info_score_threshold "
            "but an incomplete imputation score declaration; missing " + ", ".join(missing)
        )
    if any(declaration.values()) and value in ("", "NaN"):
        errors.append(
            f"analysis {analysis_id!r} has an imputation score declaration "
            "but no numeric info_score_threshold"
        )
    if complete and declaration["imputation_score_kind"] not in INFO_SCORE_KINDS:
        errors.append(
            f"analysis {analysis_id!r} has invalid imputation_score_kind "
            f"{declaration['imputation_score_kind']!r}; expected one of {sorted(INFO_SCORE_KINDS)}"
        )
    return errors


def _maf_threshold_errors(row: dict[str, str]) -> list[str]:
    """Literal NaN, or a finite number in [0, 0.5] (stores #176).

    The column is a request, not a report of what was filtered, and its range is
    the whole truth checkable from the bundle alone.
    """
    analysis_id = row.get("analysis_id") or "<unknown analysis_id>"
    value = (row.get(MAF_THRESHOLD_COLUMN) or "").strip()
    if value == "NaN":
        return []
    try:
        number = Decimal(value) if value else Decimal("NaN")
    except InvalidOperation:
        number = Decimal("NaN")
    if number.is_finite() and 0 <= number <= Decimal("0.5"):
        return []
    return [f"analysis {analysis_id!r} has maf_threshold {value!r}; expected literal NaN or a number in [0,0.5]"]


__all__ = [
    "INFO_SCORE_COLUMNS",
    "INFO_SCORE_KINDS",
    "MAF_THRESHOLD_COLUMN",
    "REQUIRED_COLUMNS",
    "check",
    "check_with_contract",
]
