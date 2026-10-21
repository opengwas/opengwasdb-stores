#!/usr/bin/env python3
"""SomaScan target annotation for the GWAS Catalog Source Metadata Table (#300, ADR 0036).

A SomaScan pQTL Analysis (Sun 2018) measures one assay, a SomaScan SeqId. The
tracked target table -- ``resources/reference-resources/somascan/sun-2018-analysis-targets.tsv``,
one or more rows per Analysis -- resolves the assay to a gene and its GRCh38
span, and SomaLogic's ``somascan-targets.tsv`` says whether an assay measures
several targets. This adapter writes the GWAS Catalog Source Metadata Table's
rows for the Analyses the target table names, with that annotation in its
target columns, so a Sun-style release needs only a release config: its cis
region policy reads the span, and the candidate renders the target (#300).

Per Analysis, as the R ``gwas-ssf-ragged`` generator read the table:

- ``target_assay_id`` is the source's own SeqId (``source_seqid``);
- the **mapped rows** are those not ``unresolved`` with a chromosome and span.
  When they name one gene, it is the target: ``target_symbol``,
  ``target_ensembl_gene_id`` and the span. When they name several (a complex,
  such as AMP kinase's three subunits on two chromosomes), one span cannot
  describe the target, so none is written and a cis policy refuses the
  Analysis by name;
- ``target_mhc`` is ``true`` when any mapped row overlaps the MHC;
- ``target_is_aggregate`` is ``true`` when SomaLogic flags the assay
  (``somascan_is_multiple``, by the matched SeqId, else the source's) or the
  source trait label lists more than one member symbol before the SeqId: the
  Analysis Label is then the assay, never one member gene (issue #141);
- ``target_resolution_method`` is the mapped rows' methods, else the rows';
  ``target_rows`` counts the mapped rows.

The other columns are carried verbatim; the table written is loaded back
through the canonical loader, and one it refuses is removed.

Usage:
    python3 -m manifest_generation.providers.gwas_catalog.somascan_targets \\
        --source-metadata-table <the GWAS Catalog's> \\
        --analysis-targets resources/reference-resources/somascan/sun-2018-analysis-targets.tsv \\
        --somascan-targets resources/reference-resources/somascan/somascan-targets.tsv --out <table>
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

from manifest_generation.connector_tsv import write_connector
from manifest_generation.region_filter import clean_chromosome
from manifest_generation.source_inventory import InventoryError
from manifest_generation.source_metadata_table import (
    COLUMNS,
    SOURCE_METADATA_TABLE,
    load_source_metadata_table,
)

#: The target columns this adapter writes.
TARGET_COLUMNS: tuple[str, ...] = (
    "target_assay_id", "target_symbol", "target_ensembl_gene_id", "target_chromosome",
    "target_start", "target_end", "target_mhc", "target_is_aggregate",
    "target_resolution_method", "target_rows",
)

#: R's TRUE spellings for the tables' logical columns.
_TRUE: frozenset[str] = frozenset({"TRUE", "true", "1"})

# The first parenthesised group of a GWAS Catalog SomaScan trait label: its
# member symbols and SeqId, "PDK1.5227.60.3" (R's source_target_symbol_count).
_LABEL_MEMBERS = re.compile(r"[^(]*\(([^)]*)\)")


class SomascanTargetError(InventoryError):
    """The target tables cannot annotate the Source Metadata Table."""


def member_symbol_count(source_label: str) -> int:
    """How many distinct member symbols a SomaScan trait label lists before its SeqId."""
    match = _LABEL_MEMBERS.match(source_label)
    if match is None:
        return 1
    tokens = {token for token in match.group(1).split(".") if token and not token.isdigit()}
    return len(tokens)


def annotate_targets(table_path: Path, analysis_targets: Path, somascan_targets: Path, out: Path) -> None:
    """Write ``table_path``'s rows for the Analyses ``analysis_targets`` names, with their targets."""
    table = load_source_metadata_table(table_path)
    by_analysis: dict[str, list[Mapping[str, str]]] = {}
    for row in _read(analysis_targets):
        by_analysis.setdefault(row["source_analysis_id"], []).append(row)
    multiple = {row["seqid"]: row["somascan_is_multiple"] in _TRUE for row in _read(somascan_targets)}
    unknown = sorted(set(by_analysis) - set(table.analysis_ids))
    if unknown:
        raise SomascanTargetError(
            f"{analysis_targets} names {len(unknown)} Analysis/Analyses the Source Metadata Table "
            f"{table_path} does not: {', '.join(unknown[:10])}"
        )
    rows = [
        {**row, **_annotation(by_analysis[row["source_analysis_id"]], multiple)}
        for row in table.rows
        if row["source_analysis_id"] in by_analysis
    ]
    columns = [column for column in COLUMNS if column in table.columns or column in TARGET_COLUMNS]
    write_connector(out, SOURCE_METADATA_TABLE, columns, rows)
    try:
        load_source_metadata_table(out)
    except InventoryError:
        Path(out).unlink()
        raise


def _annotation(rows: Sequence[Mapping[str, str]], multiple: Mapping[str, bool]) -> dict[str, str]:
    mapped = [
        row for row in rows
        if row["mapping_status"] != "unresolved" and row["chromosome"] and row["gene_start"] and row["gene_end"]
    ]
    genes = sorted({
        (row["gene_name"], row["ensembl_gene_id"], clean_chromosome(row["chromosome"]),
         row["gene_start"], row["gene_end"])
        for row in mapped
    })
    assay = rows[0]["source_seqid"]
    matched = next((row["matched_seqid"] for row in rows if row["matched_seqid"]), "")
    aggregate = multiple.get(matched or assay, False) or any(
        member_symbol_count(row["source_label"]) > 1 for row in rows
    )
    methods = [row["target_resolution_method"] for row in (mapped or rows)]
    symbol, gene, chromosome, start, end = genes[0] if len(genes) == 1 else ("",) * 5
    return {
        "target_assay_id": assay,
        "target_symbol": symbol,
        "target_ensembl_gene_id": gene,
        "target_chromosome": chromosome,
        "target_start": start,
        "target_end": end,
        "target_mhc": ("true" if any(row["mhc"] in _TRUE for row in mapped) else "false") if mapped else "",
        "target_is_aggregate": "true" if aggregate else "false",
        "target_resolution_method": ";".join(dict.fromkeys(method for method in methods if method)),
        "target_rows": str(len(mapped)) if mapped else "",
    }


def _read(path: Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-metadata-table", required=True, type=Path)
    parser.add_argument("--analysis-targets", required=True, type=Path)
    parser.add_argument("--somascan-targets", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        annotate_targets(args.source_metadata_table, args.analysis_targets, args.somascan_targets, args.out)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Source Metadata Table {args.out} written with SomaScan targets from {args.analysis_targets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
