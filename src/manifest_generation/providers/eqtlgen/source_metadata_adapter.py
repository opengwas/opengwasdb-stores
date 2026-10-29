#!/usr/bin/env python3
"""eQTLGen metadata adapter: a BESD file set (.epi) and a sample-size table -> Source Metadata Table (#291, #314, ADR 0036).

eQTLGen's cis-eQTL summary statistics are one BESD file set holding an Analysis
per probe (an Ensembl gene). The ``.epi`` names every probe and its position and
carries no study metadata, so this adapter writes one canonical row per probe
(``manifest_generation.source_metadata_table``), takes the tissue from the
operator, never from a default, and takes each probe's sample size from the
published per-pair table:

- ``source_analysis_id`` is the bare probe id, the id ``build-ragged-besd``
  gives the Analysis when the Build Recipe passes it no ``tissue``, so the
  Store's Analysis ids are the source's own and no Store-identity column is
  declared. It is never ``<probe>::<tissue>``: an ``analysis_id`` never contains
  ``:``, and ``bundle.check()`` refuses one that does;
- ``source_probe_id`` is the ``.epi`` probe the Analysis reads;
- ``source_trait_label`` is the gene's published symbol (``--gene-symbols``), or
  the probe's own id when the file names none; it is the Analysis Label;
- the probe's position is its target (``target_chromosome``, and ``target_start``
  and ``target_end`` both the probe's base pair, a one-base span: the file gives a
  position, not the gene's extent), and the probe is the target's Ensembl gene;
- ``sample_size`` is the probe's maximum pair N **as published**: the largest
  ``NrSamples`` the per-pair table (``providers.eqtlgen.sample_size_table``, the
  table ``build-ragged-besd --sample-size-table`` stores as the ``n`` plane) holds
  for the probe. It is never recovered from the BESD's beta and SE, so it agrees
  with the Store's own ``n`` plane (spec section 8 forbids presenting recovered N
  as observed). A probe the table has no pair for, or a row whose N is not a
  whole number from 1, fails the adapter by name;
- ``study_design`` is ``quantitative``, ``tissue`` is the ``--tissue`` the
  operator states, and nothing else is declared: no ancestry label (Assigned
  Ancestry is measured), no build (the release config's default) and no trait
  ontology term (a gene is not the Trait, #141). The tissue is recorded in its
  own column, never in the id, and a blank or padded one is refused before
  anything is written (issue #319).

A probe the table cannot describe -- a chromosome outside ``1``-``22``, ``X``,
``Y``, ``MT``, a repeated probe, a short row -- fails the whole adapter by line
and probe, and a table the canonical loader refuses is removed, so the adapter
never leaves one behind for freeze.

Usage:
    python3 -m manifest_generation.providers.eqtlgen.source_metadata_adapter \\
        --prefix /data/opengwasdb/raw/eqtlgen/full-19250 --tissue whole_blood \\
        --sample-size-table /data/opengwasdb/raw/eqtlgen/full-19250.sample-size.tsv.gz \\
        --first-author Võsa --publication-pmid 34475573 --gene-symbols <gene-symbols.tsv> \\
        --out <source-metadata.tsv>
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path
from typing import Sequence

import pandas as pd
import yaml

from manifest_generation.connector_tsv import raise_problems, write_connector
from manifest_generation.source_inventory import InventoryError, atomic_write, sha256_file
from manifest_generation.source_metadata_table import (
    SOURCE_METADATA_TABLE,
    TARGET_CHROMOSOMES,
    load_source_metadata_table,
)

#: The canonical columns the adapter writes.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "source_analysis_id",
    "source_probe_id",
    "source_trait_label",
    "study_design",
    "sample_size",
    "first_author",
    "publication_pmid",
    "tissue",
    "target_ensembl_gene_id",
    "target_chromosome",
    "target_start",
    "target_end",
)

#: ``.epi`` columns: chromosome, probe, genetic distance, base pair, gene, orientation.
EPI_COLUMNS = 6

#: The sample-size table's columns this reads.
TABLE_PROBE_COLUMN, TABLE_N_COLUMN = "probe_id", "n"
#: The largest N a Store holds (opengwasdb's ``N_MISSING`` is the next code).
N_MAX = 2**32 - 2
#: Table rows read at a time.
TABLE_CHUNK_ROWS = 10_000_000


class AdapterError(InventoryError):
    """The ``.epi`` cannot be converted into a valid Source Metadata Table."""


def convert(
    prefix: Path,
    out: Path,
    *,
    tissue: str,
    first_author: str,
    publication_pmid: str,
    gene_symbols: Path,
    sample_size_table: Path,
) -> None:
    """Convert the file set at ``prefix`` into a canonical Source Metadata Table at ``out``, then load it back.

    ``first_author`` and ``publication_pmid`` are the study's, written on every row;
    ``gene_symbols`` is the connector input mapping a probe to eQTLGen's published
    gene symbol, which becomes the Analysis Label (a probe with none keeps its
    Ensembl id); ``sample_size_table`` is the per-pair table each probe's ``sample_size``
    is read from (its maximum). ``<out>.provenance.yaml`` records the arguments, the
    table's sha256 and row count, and the label counts.
    """
    prefix, out = Path(prefix), Path(out)
    epi = Path(f"{prefix}.epi")
    if not epi.is_file():
        raise AdapterError(f"eQTLGen .epi not found: {epi}")
    if not tissue.strip() or tissue != tissue.strip():
        raise AdapterError(f"tissue {tissue!r} is empty or padded")
    if not first_author.strip() or first_author != first_author.strip():
        raise AdapterError(f"first_author {first_author!r} is empty or padded")
    if not publication_pmid.isascii() or not publication_pmid.isdigit():
        raise AdapterError(f"publication_pmid {publication_pmid!r} is not a PubMed id (digits only)")
    symbols = load_gene_symbols(Path(gene_symbols))
    rows: list[dict[str, str]] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for line_number, line in enumerate(epi.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != EPI_COLUMNS:
            problems.append(f"line {line_number}: {len(fields)} column(s), not {EPI_COLUMNS}")
            continue
        chromosome, probe, _distance, position, gene, _orientation = (f.strip() for f in fields)
        label = probe or f"line {line_number}"
        row_problems = []
        if not probe:
            row_problems.append("no probe id")
        elif probe in seen:
            row_problems.append(f"probe repeated (first on line {seen[probe]})")
        else:
            seen[probe] = line_number
        if chromosome not in TARGET_CHROMOSOMES:
            row_problems.append(f"chromosome {chromosome!r} is not one of {', '.join(TARGET_CHROMOSOMES)}")
        if not position.isascii() or not position.isdigit() or int(position) < 1:
            row_problems.append(f"base pair {position!r} is not a positive whole number")
        problems.extend(f"line {line_number} ({label}): {problem}" for problem in row_problems)
        if row_problems:
            continue
        rows.append({
            "source_analysis_id": probe,
            "source_probe_id": probe,
            "source_trait_label": symbols.get(probe) or probe,
            "first_author": first_author,
            "publication_pmid": publication_pmid,
            "study_design": "quantitative",
            "sample_size": "",
            "tissue": tissue,
            "target_ensembl_gene_id": probe,
            "target_chromosome": chromosome,
            "target_start": position,
            "target_end": position,
        })
    if not rows and not problems:
        problems.append("no probes")
    if problems:
        raise_problems(AdapterError, f"eQTLGen .epi {epi}", problems)
    maxima, table_rows = probe_maximum_n(Path(sample_size_table), [row["source_probe_id"] for row in rows])
    for row in rows:
        row["sample_size"] = str(maxima[row["source_probe_id"]])
    write_connector(out, SOURCE_METADATA_TABLE, OUTPUT_COLUMNS, rows)
    try:
        load_source_metadata_table(out)
    except InventoryError:
        out.unlink()
        raise
    labelled = sum(1 for row in rows if row["source_trait_label"] != row["source_probe_id"])
    atomic_write(
        Path(f"{out}.provenance.yaml"),
        yaml.safe_dump(
            {
                "written_by": __spec__.name if __spec__ else "source_metadata_adapter",
                "prefix": str(prefix),
                "tissue": tissue,
                "first_author": first_author,
                "publication_pmid": publication_pmid,
                "probes": len(rows),
                "labelled_with_symbol": labelled,
                "labelled_with_ensembl_id": len(rows) - labelled,
                "sample_size_table": {
                    "path": str(sample_size_table),
                    "rows": table_rows,
                    "sha256": sha256_file(Path(sample_size_table)),
                },
                "gene_symbols": {
                    "path": str(gene_symbols),
                    "rows": len(symbols),
                    "sha256": sha256_file(Path(gene_symbols)),
                },
            },
            sort_keys=False, allow_unicode=True,
        ),
    )


def load_gene_symbols(path: Path) -> dict[str, str]:
    """The ``gene``/``gene_symbol`` connector input as ``{gene: symbol}``; a gene given two symbols is refused."""
    if not path.is_file():
        raise AdapterError(f"gene symbols not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].split("\t") != ["gene", "gene_symbol"]:
        raise AdapterError(f"{path}: header must be 'gene<TAB>gene_symbol'")
    seen: dict[str, str] = {}
    conflicts: dict[str, set[str]] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0].strip() or not fields[1].strip():
            raise AdapterError(f"{path}: malformed row {line!r}")
        gene, symbol = fields[0].strip(), fields[1].strip()
        if gene in seen and seen[gene] != symbol:
            conflicts.setdefault(gene, {seen[gene]}).add(symbol)
        seen.setdefault(gene, symbol)
    if conflicts:
        raise_problems(
            AdapterError, f"gene symbols {path}",
            [f"{gene}: conflicting symbols {', '.join(sorted(names))}" for gene, names in sorted(conflicts.items())],
        )
    return seen


def probe_maximum_n(table: Path, probes: Sequence[str]) -> tuple[dict[str, int], int]:
    """Each probe's maximum N in the per-pair table, and the table's row count.

    ``table`` is ``sample_size_table``'s output: TSV(.gz) with ``probe_id``, ``snp_id``
    and ``n``. Rows of probes the file set does not hold are ignored. A probe with no
    row, and an N that is not a whole number from 1, are refused by name.
    """
    if not table.is_file():
        raise AdapterError(f"sample-size table not found: {table}")
    wanted = set(probes)
    best: dict[str, int] = {}
    n_rows = 0
    with open(table, "rb") as fh:
        compression = "gzip" if fh.read(2) == b"\x1f\x8b" else None
    with (gzip.open(table, "rt", encoding="utf-8") if compression else open(table, encoding="utf-8")) as fh:
        header = fh.readline()
    columns = header.rstrip("\r\n").split("\t")
    if TABLE_PROBE_COLUMN not in columns or TABLE_N_COLUMN not in columns:
        raise AdapterError(f"{table}: header {columns[:6]} has no {TABLE_PROBE_COLUMN} and {TABLE_N_COLUMN} columns")
    with pd.read_csv(
        table, sep="\t", usecols=[TABLE_PROBE_COLUMN, TABLE_N_COLUMN],
        dtype={TABLE_PROBE_COLUMN: str, TABLE_N_COLUMN: "float64"}, na_values=["NA", ""], keep_default_na=False,
        chunksize=TABLE_CHUNK_ROWS, compression=compression,
    ) as reader:
        for chunk in reader:
            n_rows += len(chunk)
            ours = chunk[chunk[TABLE_PROBE_COLUMN].isin(wanted)]
            n = ours[TABLE_N_COLUMN]
            bad = ~((n == n.round()) & (n >= 1) & (n <= N_MAX))  # NaN compares false, so it is bad too
            if bad.any():
                first = ours[bad].iloc[0]
                raise AdapterError(
                    f"{table}: {first[TABLE_PROBE_COLUMN]}: n {first[TABLE_N_COLUMN]!r} is not a whole number "
                    f"from 1 ({int(bad.sum())} such rows in the rows ending at {n_rows})"
                )
            for probe, value in ours.groupby(TABLE_PROBE_COLUMN)[TABLE_N_COLUMN].max().items():
                if value > best.get(probe, 0):
                    best[probe] = int(value)
    absent = [probe for probe in probes if probe not in best]
    if absent:
        raise_problems(AdapterError, f"sample-size table {table}", [f"{probe}: no pair with an N" for probe in absent])
    return best, n_rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--prefix", required=True, type=Path, help="the BESD file set's prefix (only its .epi is read)")
    parser.add_argument("--tissue", required=True, help="the measured tissue, e.g. whole_blood")
    parser.add_argument("--first-author", required=True, help="the study's first author, written on every row")
    parser.add_argument("--publication-pmid", required=True, help="the study's PubMed id, written on every row")
    parser.add_argument(
        "--gene-symbols", required=True, type=Path,
        help="the gene/gene_symbol connector input for the Analysis Labels (providers.eqtlgen.gene_symbols)",
    )
    parser.add_argument(
        "--sample-size-table", required=True, type=Path,
        help="the per-pair N table (providers.eqtlgen.sample_size_table): each probe's sample_size is its maximum",
    )
    parser.add_argument("--out", required=True, type=Path, help="the Source Metadata Table to write")
    args = parser.parse_args(argv)
    try:
        convert(
            args.prefix, args.out, tissue=args.tissue, first_author=args.first_author,
            publication_pmid=args.publication_pmid, gene_symbols=args.gene_symbols,
            sample_size_table=args.sample_size_table,
        )
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Source Metadata Table {args.out} written from {args.prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
