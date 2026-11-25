"""Generate `ld-resources.tsv` from the LD Store Registry's records, and STORES.md's LD section.

The index reads the records alone, never a Store. A record that breaks the
spec stops it before anything is written, naming the record and the field.
`workflow/Snakefile`'s `index` rule calls `generate_ld_index`, then hands the
section it returns to `ogstores.index.generate_index`, which writes `STORES.md`:
each file has one writer.
"""

from __future__ import annotations

from pathlib import Path

from ld_registry.record import RecordError, load_records
from ld_registry.rows import render_md_section, render_tsv

TSV_FILENAME = "ld-resources.tsv"


class InvalidRecords(ValueError):
    """The registry holds a record that breaks the spec."""

    def __init__(self, errors: list[RecordError]) -> None:
        self.errors = errors
        super().__init__("the LD Store Registry has invalid records:\n" + "\n".join(f"  {e}" for e in errors))


def generate_ld_index(ld_root: Path | str, repo_root: Path | str) -> str:
    """Write `<repo_root>/ld-resources.tsv` from the records under `ld_root`; return the STORES.md section."""
    records, errors = load_records(Path(ld_root))
    if errors:
        raise InvalidRecords(errors)
    (Path(repo_root) / TSV_FILENAME).write_text(render_tsv(records), encoding="utf-8")
    return render_md_section(records)

