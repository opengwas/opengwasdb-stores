"""Generate `ld-resources.tsv` from the LD Store Registry's records, and STORES.md's LD section.

The index reads the records alone, never a Store. A record that breaks the
spec stops it before anything is written, naming the record and the field; so
does a link whose target no live record holds (`link_errors`), reported beside
any record's errors. A link is checked against the valid records alone.
`workflow/Snakefile`'s `index` rule calls `generate_ld_index`, then hands the
section it returns to `ogstores.index.generate_index`, which writes `STORES.md`:
each file has one writer.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ogstores.resources import REFERENCE_RESOURCES_DIR

from ld_registry.panels import panel_rows
from ld_registry.record import InvalidRecords, valid_records
from ld_registry.rows import Value, merged_values, render_md_section, render_tsv

#: `InvalidRecords` is what `resource_rows` and `generate_ld_index` raise for a record that breaks the spec.
__all__ = ["IdCollision", "InvalidRecords", "TSV_FILENAME", "generate_ld_index", "resource_rows", "resources_root_of"]

TSV_FILENAME = "ld-resources.tsv"


class IdCollision(ValueError):
    """An LD panel's id is a record's: an id is assigned once (ADR 0022, ADR 0042)."""


def resources_root_of(ld_root: Path | str) -> Path:
    """The `resources/reference-resources` directory of the repository whose records are under `ld_root`:
    the one place that finds the LD panels' declarations (the index, the Analysis Catalog, the Snakefile)."""
    return Path(ld_root).parent.parent / REFERENCE_RESOURCES_DIR


def _sources(ld_root: Path | str, resources_root: Path | str) -> tuple[list[dict[str, Any]], list[dict[str, Value]]]:
    """The valid records and the LD panels' rows; an invalid record, or an id both claim, is refused."""
    records = valid_records(ld_root)
    panels = panel_rows(resources_root)
    record_ids = {record["ld_id"] for record in records}
    clashes = [f"{row['ld_id']} is a record's and the panel {row['label']}'s" for row in panels if row["ld_id"] in record_ids]
    if clashes:
        raise IdCollision("an LD id is assigned once:\n" + "\n".join(f"  {c}" for c in clashes))
    return records, panels


def resource_rows(ld_root: Path | str, resources_root: Path | str) -> list[Mapping[str, Value]]:
    """Every row of `ld-resources.tsv`, typed and in `COLUMNS` order, in id order: the records' and the LD panels'.

    The one list the TSV and the Analysis Catalog's `ld_resources` table both take.
    """
    return merged_values(*_sources(ld_root, resources_root))


def generate_ld_index(ld_root: Path | str, repo_root: Path | str, resources_root: Path | str) -> str:
    """Write `<repo_root>/ld-resources.tsv` from the records under `ld_root` and the LD panels declared
    under `resources_root` (a `resources/reference-resources` directory); return the STORES.md section."""
    records, panels = _sources(ld_root, resources_root)
    (Path(repo_root) / TSV_FILENAME).write_text(render_tsv(records, panels), encoding="utf-8")
    return render_md_section(records, panels)
