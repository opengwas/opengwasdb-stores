"""An LD Store record's values in the index's columns, and their two renderings.

`ld_values` gives each record's typed values in `COLUMNS` order, with `None`
for a value the record does not hold; the Analysis Catalog's table (#537)
reads them as they are. `render_tsv` writes `ld-resources.tsv` from them, a
`None` as an empty cell, never `0` and never `-`; `render_md_section` writes
STORES.md's LD section. Nothing here opens a Store.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from typing import Any

from ld_registry.record import LD_ROOT, record_path

Value = int | float | str | None

COLUMNS: tuple[str, ...] = (
    "ld_id",
    "label",
    "kind",
    "provenance",
    "status",
    "used_for",
    "ancestry",
    "source_panel",
    "n",
    "k",
    "reference_assembly",
    "format_version",
    "representation",
    "n_variants",
    "n_blocks",
    "store_bytes",
    "genotype_bytes",
    "r_scale",
    "near_bp",
    "far_bp",
    "t_far",
    "t_partner",
    "created_at",
    "build_wall_s",
    "built_with",
    "source",
    "licence",
    "location",
    "record",
    "issue",
    "notes",
)

#: STORES.md's LD section: (heading, column), a subset of `COLUMNS`.
MD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("LD ID", "ld_id"),
    ("Label", "label"),
    ("Kind", "kind"),
    ("Status", "status"),
    ("Ancestry", "ancestry"),
    ("n", "n"),
    ("k", "k"),
    ("Format", "format_version"),
    ("Variants", "n_variants"),
    ("Bytes", "store_bytes"),
    ("Licence", "licence"),
)
NUMERIC_MD_COLUMNS = frozenset({"n", "k", "n_variants", "store_bytes"})


def representation(kind: Any, provenance: Mapping[str, Any]) -> str | None:
    """What a Store holds, by its kind first: an LD Store holds pairwise r in its
    Near Band and Far Pairs, and in Partner Rows when it records a `t_partner`.
    A Genotype Store's is #539's to say."""
    if kind != "ld_store":
        return None
    parts = ["Near Band", "Far Pairs"] + (["Partner Rows"] if provenance.get("t_partner") is not None else [])
    return f"pairwise r ({', '.join(parts)})"


def ld_values(record: Mapping[str, Any]) -> dict[str, Value]:
    """One valid record's values, typed, in `COLUMNS` order; `None` where the record holds none."""
    provenance: Mapping[str, Any] = record.get("provenance") or {}
    rebuild: Mapping[str, Any] = record.get("rebuild") or {}
    measured: Mapping[str, Any] = record.get("measured") or {}
    wall = provenance.get("wall_seconds")
    values: dict[str, Value] = {
        "ld_id": record.get("ld_id"),
        "label": record.get("label"),
        "kind": record.get("kind"),
        "provenance": provenance.get("source_kind"),
        "status": record.get("status"),
        "used_for": record.get("used_for"),
        "ancestry": provenance.get("ancestry"),
        "source_panel": provenance.get("source_panel"),
        "n": provenance.get("n"),
        "k": provenance.get("k"),
        "reference_assembly": provenance.get("reference_assembly"),
        "format_version": provenance.get("format_version"),
        "representation": representation(record.get("kind"), provenance),
        "n_variants": provenance.get("n_variants"),
        "n_blocks": None,  # an LD panel's (#540)
        "store_bytes": measured.get("store_bytes"),
        "genotype_bytes": None,  # a Genotype Store's (#539)
        "r_scale": provenance.get("r_scale"),
        "near_bp": provenance.get("near_bp"),
        "far_bp": provenance.get("far_bp"),
        "t_far": provenance.get("t_far"),
        "t_partner": provenance.get("t_partner"),
        "created_at": provenance.get("created_at"),
        "build_wall_s": None if wall is None else round(wall),
        "built_with": f"ogld {provenance['software']['ogld']} ({rebuild.get('script')})",
        "source": provenance.get("source_description"),
        "licence": record.get("licence"),
        "location": record.get("location"),
        "record": record_path(LD_ROOT, str(record.get("ld_id"))).as_posix(),
        "issue": record.get("issue"),
        "notes": record.get("notes"),
    }
    return {column: values[column] for column in COLUMNS}


def ld_row(record: Mapping[str, Any]) -> dict[str, str]:
    """One record's `ld-resources.tsv` row: each value as text, `None` as an empty cell."""
    return {column: "" if value is None else str(value) for column, value in ld_values(record).items()}


def render_tsv(records: Iterable[Mapping[str, Any]]) -> str:
    """`ld-resources.tsv`: a header, then one row per record in id order."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for record in sorted(records, key=lambda r: str(r.get("ld_id"))):
        writer.writerow(ld_row(record))
    return output.getvalue()


def _md_cell(column: str, value: Value) -> str:
    if value is None or value == "":
        return "-"
    if column == "ld_id":
        return f"`{value}`"
    if column in NUMERIC_MD_COLUMNS:
        return f"{value:,}"
    return str(value).replace("|", "\\|")


def render_md_section(records: Iterable[Mapping[str, Any]]) -> str:
    """`STORES.md`'s LD section, which `ogstores.index.generate_index` places after the Store Releases."""
    lines = [
        "## LD resources",
        "",
        "Generated from the LD Store Registry's records in `stores/ld/` (docs/spec/ld-store-registry.md). "
        "Every column is in `ld-resources.tsv`. Do not edit by hand; regenerate with `pixi run index`.",
        "",
        "| " + " | ".join(heading for heading, _ in MD_COLUMNS) + " |",
        "|" + "|".join("---:" if column in NUMERIC_MD_COLUMNS else ":---" for _, column in MD_COLUMNS) + "|",
    ]
    for record in sorted(records, key=lambda r: str(r.get("ld_id"))):
        values = ld_values(record)
        lines.append("| " + " | ".join(_md_cell(column, values[column]) for _, column in MD_COLUMNS) + " |")
    lines.append("")
    return "\n".join(lines)
