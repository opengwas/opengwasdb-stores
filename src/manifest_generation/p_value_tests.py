"""The reviewed per-Analysis tables opengwasdb#371 added to a release's policy.

A Store keeps z (= beta/se) and answers a query with the p of z, so the
resolver reads each source's p only to check beta/se against it
(opengwasdb ADR 0069). That check is meaningful only when the p is the Wald p
of the stored coefficient. The engine therefore requires every resolver
manifest row to declare ``p_value_test`` (opengwasdb ADR 0085):

- ``wald_1df`` is checked;
- ``joint_2df`` skips the check;
- ``mr_mega`` is refused unread.

The owner ruled on 20 Nov 2026 to drop the MR-MEGA Analyses, and to give the
Analyses whose low-frequency rows contradict their p a higher MAF floor.

``release_policy.p_value_tests`` lists every Analysis whose p is not a
``wald_1df``, and any Analysis whose label suggests otherwise but is one. Every
unlisted Analysis is ``wald_1df``, unless its Source Trait Label names a 2-df
test or MR-MEGA. Such an Analysis is refused until the table declares it, so a
new release cannot take the ``wald_1df`` reading by silence.

``release_policy.maf_threshold_overrides`` replaces ``defaults.maf_threshold``
for the Analyses it lists. Each value is the one #371's whole-file measurement
chose, and each reason records it.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

__all__ = [
    "MAF_THRESHOLD_OVERRIDE_COLUMNS",
    "P_VALUE_TEST_COLUMNS",
    "P_VALUE_TESTS",
    "ReviewedValue",
    "ReviewedTableError",
    "declared_p_value_test",
    "read_maf_threshold_overrides",
    "read_p_value_tests",
]

#: The values the engine accepts (opengwasdb ``PValueTest``).
P_VALUE_TESTS: tuple[str, ...] = ("wald_1df", "joint_2df", "mr_mega")
#: The test an Analysis the table does not list is read as.
UNLISTED_P_VALUE_TEST = "wald_1df"
P_VALUE_TEST_COLUMNS: tuple[str, ...] = ("analysis_id", "p_value_test", "reason")
MAF_THRESHOLD_OVERRIDE_COLUMNS: tuple[str, ...] = ("analysis_id", "maf_threshold", "reason")

#: A Source Trait Label that names a test other than a 1-df Wald test: "(2df test)",
#: "(2df)", "2-df", "(MR-MEGA)".
_NON_WALD_LABEL = re.compile(r"\b2\s*-?\s*df\b|\bmr-?mega\b", re.IGNORECASE)


class ReviewedTableError(ValueError):
    """A reviewed table that cannot be read as written."""


@dataclass(frozen=True)
class ReviewedValue:
    """One reviewed row: the value and the reason a person recorded for it."""

    value: str
    reason: str


def read_p_value_tests(path: Path | None) -> dict[str, ReviewedValue]:
    """``analysis_id -> (p_value_test, reason)``, or ``{}`` when the release names no table."""
    return _read(path, P_VALUE_TEST_COLUMNS, "p-value test", _p_value_test)


def read_maf_threshold_overrides(path: Path | None) -> dict[str, ReviewedValue]:
    """``analysis_id -> (maf_threshold, reason)``, or ``{}`` when the release names no table."""
    return _read(path, MAF_THRESHOLD_OVERRIDE_COLUMNS, "MAF threshold override", _maf_threshold)


def declared_p_value_test(
    analysis_id: str, source_label: str, declared: dict[str, ReviewedValue] | None
) -> str:
    """The ``p_value_test`` cell for one Analysis.

    A listed Analysis takes its reviewed value. An unlisted one is ``wald_1df``,
    unless its label names another test: that is refused, naming the label.
    """
    found = (declared or {}).get(analysis_id)
    if found is not None:
        return found.value
    if _NON_WALD_LABEL.search(source_label):
        raise ReviewedTableError(
            f"{analysis_id}: its label {source_label!r} names a test other than a 1-df Wald "
            "test, and release_policy.p_value_tests does not declare its p_value_test "
            "(opengwasdb#371). Measure what its p_value column holds and list it."
        )
    return UNLISTED_P_VALUE_TEST


def _p_value_test(raw: str) -> str:
    if raw not in P_VALUE_TESTS:
        raise ValueError(f"p_value_test must be one of {', '.join(P_VALUE_TESTS)}, not {raw!r}")
    return raw


def _maf_threshold(raw: str) -> str:
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"maf_threshold must be a number in (0, 0.5], not {raw!r}") from exc
    if not number.is_finite() or not 0 < number <= Decimal("0.5"):
        raise ValueError(f"maf_threshold must be a number in (0, 0.5], not {raw!r}")
    return raw


def _read(
    path: Path | None, columns: tuple[str, ...], what: str, check: Callable[[str], str]
) -> dict[str, ReviewedValue]:
    if path is None:
        return {}
    if not path.is_file():
        raise ReviewedTableError(f"{what} table not found: {path}")
    found: dict[str, ReviewedValue] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if reader.fieldnames != list(columns):
            raise ReviewedTableError(f"{path}: expected exact TSV headers {columns!r}")
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ReviewedTableError(f"{path}:{number}: malformed {what} row")
            analysis_id, raw, reason = (row[name].strip() for name in columns)
            if not analysis_id or not reason:
                raise ReviewedTableError(
                    f"{path}:{number}: analysis_id and reason must be non-empty"
                )
            if analysis_id in found:
                raise ReviewedTableError(f"{path}:{number}: duplicate analysis_id {analysis_id!r}")
            try:
                value = check(raw)
            except ValueError as exc:
                raise ReviewedTableError(f"{path}:{number}: {exc}") from exc
            found[analysis_id] = ReviewedValue(value, reason)
    return found
