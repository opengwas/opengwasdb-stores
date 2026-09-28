#!/usr/bin/env python3
"""Derive reviewed imputation-score declarations from source headers.

The Phase B candidate generator consumes an explicit, reviewed per-Analysis
score declaration TSV (``source.imputation_score_declarations``).  That file
must not be an ad-hoc survey: this command scans the header of every *ready*
row in the frozen Source Inventory, applies one fixed, case-sensitive synonym
table, and writes the declarations deterministically.

The table below is the stores-side OGS-00011 admission contract (issue #176).
A synonym is matched exactly, never as a substring or case-insensitively, so a
look-alike such as ``additional_info`` or ``r2_iCOGS`` never becomes a score.
The first synonym declared, in the listed order, wins: every ``imputation_info``
synonym is preferred over every ``imputation_r2`` synonym.

    pixi run python resources/generators/gwas-catalog-eur-hybrid/derive_score_declarations.py

Writes ``resources/generators/gwas-catalog-eur-hybrid/score-declarations.tsv``
(or ``--out``) and prints a per-header count.  The output columns are exactly
the resolver manifest declaration columns:

    analysis_id  imputation_score_column  imputation_score_kind  imputation_score_provenance

An Analysis whose header carries no synonym is simply absent from the output;
the candidate generator then emits literal ``NaN`` for it.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from resources.generators.lib.source_inventory import (  # noqa: E402
    SourceInventoryRow,
    read_inventory,
)

FAMILY_DIR = "resources/generators/gwas-catalog-eur-hybrid"
DEFAULT_INVENTORY = "resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-22.tsv"
DEFAULT_OUT = f"{FAMILY_DIR}/score-declarations.tsv"

#: The exact, case-sensitive synonym table (OGS-00011 admission contract #176).
#: Order is the precedence rule: within a kind the listed order wins, and every
#: ``imputation_info`` synonym outranks every ``imputation_r2`` synonym.
SYNONYMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "imputation_info",
        (
            "info",
            "INFO",
            "Info",
            "info_score",
            "variant_info_score",
            "INFO_UKB",
            "imputationInfo",
            "info_score_from_regenie",
            "mininfo",
        ),
    ),
    ("imputation_r2", ("imputed_r2", "R2", "imp_rsqr")),
)

#: Source columns that look like a score but are explicitly not one (#176). They
#: are listed so the exclusion is a fixed, testable decision rather than a
#: by-product of exact matching.
NEVER_DECLARED: frozenset[str] = frozenset(
    {
        "additional_info",
        "lowQuality",
        "r2_iCOGS",
        "icogs2_r2",
        "R2_oncoarray",
        "Yrsq",
        "mmm_var_info_nonmissing",
    }
)

#: The ``info`` spelling's provenance is the harmonised-format specification.
INFO_PROVENANCE = (
    "GWAS Catalog summary statistics format (GWAS-SSF) defines the info field as "
    "'Imputation information metric', a number between 0 and 1 "
    "(https://www.ebi.ac.uk/gwas/docs/summary-statistics-format, retrieved 2026-09-28)"
)

#: Every other synonym's provenance is the operator approval plus the witnessed
#: spelling; the matched header is appended.
SYNONYM_PROVENANCE_TEMPLATE = (
    "Operator-approved synonym of the GWAS-SSF info field "
    "(opengwasdb-stores#176, 2026-09-28); source header '{header}'"
)


@dataclass(frozen=True)
class Declaration:
    """One derived per-Analysis score declaration."""

    analysis_id: str
    column: str
    kind: str
    provenance: str


def _provenance(header: str) -> str:
    if header == "info":
        return INFO_PROVENANCE
    return SYNONYM_PROVENANCE_TEMPLATE.format(header=header)


def declaration_for_header(header: str) -> tuple[str, str] | None:
    """Return ``(kind, provenance)`` for an exact header, or ``None``.

    Exact and case-sensitive: ``Info`` matches the ``Info`` synonym but
    ``info2``, ``additional_info`` and lowercase ``r2`` do not. ``NEVER_DECLARED``
    is checked first so a future edit to the synonym table cannot silently
    resurrect a known look-alike.
    """
    if not header or header in NEVER_DECLARED:
        return None
    for kind, synonyms in SYNONYMS:
        if header in synonyms:
            return kind, _provenance(header)
    return None


def declaration_for_columns(columns: list[str]) -> tuple[str, str, str] | None:
    """Apply the synonym table to one header; first match in listed order wins."""
    present = set(columns)
    for _kind, synonyms in SYNONYMS:
        for header in synonyms:
            if header in present:
                match = declaration_for_header(header)
                if match is not None:
                    return header, match[0], match[1]
    return None


def read_header(path: str) -> list[str]:
    """Read the first line (header) of a source file, gzipped or plain."""
    source = Path(path)
    if source.suffix == ".gz":
        with gzip.open(source, "rb") as fh:
            first = fh.readline()
    else:
        with source.open("rb") as fh:
            first = fh.readline()
    text = first.decode("utf-8", errors="replace").rstrip("\r\n")
    return text.split("\t") if text else []


def derive_declarations(rows: tuple[SourceInventoryRow, ...]) -> list[Declaration]:
    """One declaration per ready Analysis whose header names a declared score."""
    declarations: list[Declaration] = []
    for row in rows:
        if not row.ready:
            continue
        if not row.data_file:
            raise ValueError(f"{row.analysis_id}: ready row has no data_file")
        match = declaration_for_columns(read_header(row.data_file))
        if match is None:
            continue
        column, kind, provenance = match
        declarations.append(
            Declaration(
                analysis_id=row.analysis_id,
                column=column,
                kind=kind,
                provenance=provenance,
            )
        )
    declarations.sort(key=lambda declaration: declaration.analysis_id)
    return declarations


DECLARATION_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "imputation_score_column",
    "imputation_score_kind",
    "imputation_score_provenance",
)


def render_declarations(declarations: list[Declaration]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(DECLARATION_COLUMNS), delimiter="\t", lineterminator="\n"
    )
    writer.writeheader()
    for declaration in declarations:
        writer.writerow(
            {
                "analysis_id": declaration.analysis_id,
                "imputation_score_column": declaration.column,
                "imputation_score_kind": declaration.kind,
                "imputation_score_provenance": declaration.provenance,
            }
        )
    return buffer.getvalue()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--inventory", default=DEFAULT_INVENTORY, help="frozen Source Inventory TSV")
    parser.add_argument("--out", default=DEFAULT_OUT, help="declaration TSV to write")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--stdout", action="store_true", help="write the TSV to stdout instead of --out")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    inventory_path = Path(args.inventory)
    if not inventory_path.is_absolute():
        inventory_path = repo_root / inventory_path
    rows = read_inventory(inventory_path)
    declarations = derive_declarations(rows)
    text = render_declarations(declarations)

    counts: dict[tuple[str, str], int] = {}
    for declaration in declarations:
        counts[(declaration.column, declaration.kind)] = (
            counts.get((declaration.column, declaration.kind), 0) + 1
        )
    if args.stdout:
        sys.stdout.write(text)
    else:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = repo_root / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"Wrote {out_path} ({len(declarations)} declared Analyses)")
    print(f"Ready Analyses scanned: {sum(1 for row in rows if row.ready)}")
    for (column, kind), count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {column} ({kind}): {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
