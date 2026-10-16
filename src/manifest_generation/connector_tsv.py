"""Read and write the TSV connector files a Source Collection supplies (ADR 0036).

A Source Metadata Table and an Acquisition Manifest are both machine-written
TSVs with a fixed column vocabulary, each described by a :class:`ConnectorSpec`.
This module holds the one reader and the one writer they share, so both refuse
a malformed file the same way: an unknown column, a missing required column, a
duplicate header name, a ragged row, a padded value or an empty file is an
error, never a row to skip or a cell to trim.

It also holds the error every freeze refusal descends from, and the one rule
for how many problems a refusal lists.

Only the shape of a file lives here. What each column means and which values it
admits belong to the module that owns the file (``source_metadata_table`` and
``acquisition_manifest``).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from manifest_generation.source_inventory import InventoryError, atomic_write

#: How many individual problems or identifiers one refusal lists before summarising the rest.
MAX_LISTED: int = 20


class FreezeError(InventoryError):
    """The Source Inventory is not frozen: a connector file, the Selection or their reconciliation is refused.

    Each source of refusal has its own subclass (``SourceMetadataTableError``,
    ``AcquisitionManifestError``, ``SelectionError``, ``ReconciliationError``),
    so a caller can tell which input to fix; all of them are ``FreezeError``
    and therefore ``InventoryError``, which the inventory commands report.
    """


@dataclass(frozen=True)
class ConnectorSpec:
    """The column contract of one connector file."""

    what: str
    columns: tuple[str, ...]
    required_columns: tuple[str, ...]
    error: type[FreezeError]


def listed(values: Sequence[str]) -> str:
    """``values`` joined for a message, at most :data:`MAX_LISTED` of them."""
    shown = ", ".join(values[:MAX_LISTED])
    more = len(values) - MAX_LISTED
    return shown + (f", ... and {more} more" if more > 0 else "")


def raise_problems(error: type[InventoryError], subject: str, problems: Sequence[str]) -> None:
    """Raise ``error`` naming every problem (up to :data:`MAX_LISTED`), so one pass fixes them all."""
    shown = list(problems[:MAX_LISTED])
    if len(problems) > MAX_LISTED:
        shown.append(f"... and {len(problems) - MAX_LISTED} more")
    raise error(f"{subject} is refused ({len(problems)} problem(s)): " + "; ".join(shown))


def read_connector(path: Path, spec: ConnectorSpec) -> tuple[tuple[str, ...], list[tuple[int, dict[str, str]]]]:
    """Return ``(header, [(line_number, row), ...])`` or raise ``spec.error``.

    Values are returned verbatim. A connector is written by a program, so a
    value padded with whitespace is that program's defect and is refused
    rather than trimmed; so is a row whose field count differs from the
    header's.
    """
    path = Path(path)
    if not path.is_file():
        raise spec.error(f"{spec.what} not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        try:
            header = tuple(next(reader))
        except StopIteration:
            raise spec.error(f"{spec.what} {path} is empty") from None
        problems = _header_problems(header, spec)
        if problems:
            raise spec.error(f"{spec.what} {path}: " + "; ".join(problems))
        rows: list[tuple[int, dict[str, str]]] = []
        for line_number, fields in enumerate(reader, start=2):
            if len(fields) != len(header):
                problems.append(
                    f"line {line_number} has {len(fields)} field(s), the header has {len(header)}"
                )
                continue
            row = dict(zip(header, fields))
            padded = [name for name, value in row.items() if value != value.strip()]
            if padded:
                problems.append(
                    f"line {line_number}: leading or trailing whitespace in {', '.join(padded)}"
                )
            rows.append((line_number, row))
    if problems:
        raise_problems(spec.error, f"{spec.what} {path}", problems)
    if not rows:
        raise spec.error(f"{spec.what} {path} has no rows")
    return header, rows


def render_connector(spec: ConnectorSpec, columns: Iterable[str], rows: Iterable[Mapping[str, str]]) -> str:
    """Render rows as one connector TSV with ``columns`` in the spec's canonical order.

    Refuses an unknown or missing required column, and a value that cannot be
    one TSV field or that the reader would refuse as padded. Values themselves
    are not validated: the reader is the gate, and a writer loads what it wrote
    back before handing it on.
    """
    columns = tuple(columns)
    problems = _header_problems(columns, spec)
    if problems:
        raise spec.error(f"cannot write {spec.what}: " + "; ".join(problems))
    ordered = [column for column in spec.columns if column in columns]
    lines = ["\t".join(ordered)]
    for row in rows:
        values = [row.get(column, "") or "" for column in ordered]
        for column, value in zip(ordered, values):
            if "\t" in value or "\n" in value or "\r" in value:
                raise spec.error(f"{spec.what} {column} value {value!r} contains a tab or newline")
            if value != value.strip():
                raise spec.error(f"{spec.what} {column} value {value!r} has leading or trailing whitespace")
        lines.append("\t".join(values))
    return "\n".join(lines) + "\n"


def write_connector(
    path: Path, spec: ConnectorSpec, columns: Iterable[str], rows: Iterable[Mapping[str, str]]
) -> str:
    """Write a connector file atomically, so a reader never sees half of one; return its text."""
    text = render_connector(spec, columns, rows)
    atomic_write(Path(path), text)
    return text


def _header_problems(header: Sequence[str], spec: ConnectorSpec) -> list[str]:
    problems: list[str] = []
    duplicated = sorted({name for name in header if header.count(name) > 1})
    if duplicated:
        problems.append(f"header names column(s) more than once: {', '.join(duplicated)}")
    unknown = [name for name in header if name not in spec.columns]
    if unknown:
        problems.append(f"unknown column(s): {', '.join(unknown)}")
    missing = [name for name in spec.required_columns if name not in header]
    if missing:
        problems.append(f"missing required column(s): {', '.join(missing)}")
    return problems
