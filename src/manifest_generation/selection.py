"""Selection: the subset of a Source Collection's intended Analyses one release takes (#283).

A release config declares its Selection in a ``selection`` block, exactly one of:

``all: true``
    every row of the Source Metadata Table;
``analyses: [ID, ...]``
    an explicit list of ``source_analysis_id`` values, each of which the table
    must intend;
``where: {column: value-or-list, ...}``
    a rule over the table's canonical columns: a value is equality, a list is
    membership, and the columns are combined with AND. A blank cell matches
    nothing.

The rule language stops there on purpose. Anything a rule cannot say is said
with a list, which is reviewable line by line; negation, ranges and OR would
make a Selection a program whose meaning a reviewer has to work out.

Today's generator configs carry a ``selection`` block in an older grammar
(``store_key``, ``ancestry_group``, ``n_analyses``, ``mode``...). It is
recognised and refused by name: #287 migrates every release config, and a
legacy key read as a rule would select something nobody declared.

Freeze records the declaration (:meth:`Selection.as_dict`) in the frozen Source
Inventory's provenance, so a subset is visible in the bundle rather than implied
by which metadata was used. One Selection gives one candidate Release Bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from manifest_generation.connector_tsv import FreezeError, listed
from manifest_generation.source_metadata_table import COLUMNS, SourceMetadataTable

FORMS: tuple[str, ...] = ("all", "analyses", "where")

#: The keys of the pre-#283 generator-config ``selection`` grammar, every one
#: a current config uses. #287 migrates those configs to the forms above.
LEGACY_SELECTION_KEYS: frozenset[str] = frozenset(
    {
        "ancestry_group",
        "binary_count",
        "fail_if_target_unresolved",
        "mode",
        "n_analyses",
        "probe_ids",
        "pubmed_id",
        "store_key",
        "study_design",
    }
)


class SelectionError(FreezeError):
    """A Selection is malformed, or does not fit the Source Metadata Table it selects from."""


@dataclass(frozen=True)
class Selection:
    """One parsed Selection: exactly one form, never empty.

    ``form`` is ``"all"``, ``"analyses"`` (with a non-empty ``analyses``) or
    ``"where"`` (with a non-empty ``rule`` of ``(column, values)`` pairs). Any
    other combination is refused at construction.
    """

    # Defaulted only so that ``Selection()`` reaches the refusal below instead
    # of a bare TypeError: an empty Selection is a declaration error.
    form: str = ""
    analyses: tuple[str, ...] = ()
    rule: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if self.form not in FORMS:
            raise SelectionError(f"a Selection's form is one of {', '.join(FORMS)}, not {self.form!r}")
        needs = {"all": (False, False), "analyses": (True, False), "where": (False, True)}[self.form]
        if (bool(self.analyses), bool(self.rule)) != needs:
            raise SelectionError(
                f"a {self.form!r} Selection needs "
                + {"all": "no list and no rule", "analyses": "a non-empty list and no rule",
                   "where": "a non-empty rule and no list"}[self.form]
            )

    def as_dict(self) -> dict[str, Any]:
        """The declaration as a release config writes it, for the inventory's provenance."""
        if self.form == "all":
            return {"all": True}
        if self.form == "analyses":
            return {"analyses": list(self.analyses)}
        return {
            "where": {
                column: values[0] if len(values) == 1 else list(values)
                for column, values in self.rule
            }
        }


def parse_selection(raw: Any, *, where: str = "selection") -> Selection:
    """Parse a ``selection`` block, refusing anything but exactly one well-formed form."""
    if not isinstance(raw, Mapping):
        raise SelectionError(f"{where} must be a mapping with exactly one of: {', '.join(FORMS)}")
    legacy = sorted(str(key) for key in raw if key in LEGACY_SELECTION_KEYS)
    if legacy:
        raise SelectionError(
            f"{where} is a legacy Selection (keys: {', '.join(legacy)}); the shared freeze reads "
            f"only {', '.join(FORMS)}, and #287 migrates every release config to them"
        )
    unknown = sorted(str(key) for key in raw if key not in FORMS)
    if unknown:
        raise SelectionError(f"{where} has unknown key(s): {', '.join(unknown)}")
    present = [key for key in FORMS if key in raw]
    if len(present) != 1:
        raise SelectionError(
            f"{where} must declare exactly one of: {', '.join(FORMS)} (found {len(present)})"
        )
    form = present[0]
    value = raw[form]
    if form == "all":
        if value is not True:
            raise SelectionError(f"{where}.all must be true; select a subset with 'analyses' or 'where'")
        return Selection(form="all")
    if form == "analyses":
        if not isinstance(value, list) or not value:
            raise SelectionError(f"{where}.analyses must be a non-empty list of source_analysis_id values")
        analyses = tuple(_text(item, f"{where}.analyses") for item in value)
        twice = sorted({item for item in analyses if analyses.count(item) > 1})
        if twice:
            raise SelectionError(f"{where}.analyses names {', '.join(twice)} twice")
        return Selection(form="analyses", analyses=analyses)
    if not isinstance(value, Mapping) or not value:
        raise SelectionError(f"{where}.where must be a non-empty mapping of column to value or list")
    rule: list[tuple[str, tuple[str, ...]]] = []
    for column, wanted in value.items():
        column = str(column)
        if column not in COLUMNS:
            raise SelectionError(
                f"{where}.where names {column!r}, which is not a canonical Source Metadata Table column"
            )
        values = wanted if isinstance(wanted, list) else [wanted]
        if not values:
            raise SelectionError(f"{where}.where.{column} is an empty list; it would select nothing")
        rule.append((column, tuple(_text(item, f"{where}.where.{column}") for item in values)))
    return Selection(form="where", rule=tuple(rule))


def load_selection(path: Path) -> Selection:
    """Read the ``selection`` block of a YAML file -- a release config, or a file holding only it."""
    path = Path(path)
    if not path.is_file():
        raise SelectionError(f"Selection file not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or "selection" not in document:
        raise SelectionError(f"{path} declares no 'selection' block")
    return parse_selection(document["selection"], where=f"{path}:selection")


def select(table: SourceMetadataTable, selection: Selection) -> tuple[Mapping[str, str], ...]:
    """The table rows ``selection`` takes, in table order. Selecting nothing is refused."""
    if selection.form == "analyses":
        unknown = [analysis_id for analysis_id in selection.analyses if analysis_id not in table]
        if unknown:
            raise SelectionError(
                f"Selection lists {len(unknown)} Analysis/Analyses the Source Metadata Table "
                f"{table.path} does not intend: {listed(unknown)}"
            )
        wanted = set(selection.analyses)
        selected = tuple(row for row in table.rows if row["source_analysis_id"] in wanted)
    elif selection.form == "all":
        selected = table.rows
    else:
        lacking = [column for column, _ in selection.rule if column not in table.columns]
        if lacking:
            raise SelectionError(
                f"Selection rule names {', '.join(lacking)}, which the Source Metadata Table "
                f"{table.path} does not carry"
            )
        selected = tuple(
            row
            for row in table.rows
            if all(row.get(column, "") in values for column, values in selection.rule)
        )
    if not selected:
        raise SelectionError(
            f"Selection {selection.as_dict()} selects no Analysis from {table.path}"
        )
    return selected


def _text(value: Any, where: str) -> str:
    """A rule or list value as the table's text. Only strings and integers are values."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SelectionError(f"{where} value {value!r} is not a string or an integer")
    text = str(value)
    if not text or text != text.strip():
        raise SelectionError(f"{where} value {value!r} is empty or padded with whitespace")
    return text
