#!/usr/bin/env python3
"""eQTLGen metadata adapter: the BESD ``.epi`` -> Source Metadata Table (#291, ADR 0036).

eQTLGen's cis-eQTL summary statistics are one BESD file set holding an Analysis
per probe (an Ensembl gene). The ``.epi`` names every probe and its position and
carries no study metadata, so this adapter writes one canonical row per probe
(``manifest_generation.source_metadata_table``) and takes the study-level facts
from the operator, never from a default:

- ``source_analysis_id`` is the bare probe id, the id ``build-ragged-besd``
  gives the Analysis when the Build Recipe passes it no ``tissue``, so the
  Store's Analysis ids are the source's own and no Store-identity column is
  declared. It is never ``<probe>::<tissue>``: an ``analysis_id`` never contains
  ``:`` (issue #319), and ``bundle.check()`` refuses one that does;
- ``source_probe_id`` is the ``.epi`` probe the Analysis reads;
- ``source_trait_label`` is the ``.epi`` gene column (the probe's own id: the
  file names no symbol), which is the Analysis Label;
- the probe's position is its target (``target_chromosome``, and ``target_start``
  and ``target_end`` both the probe's base pair, a one-base span: the file gives a
  position, not the gene's extent), and the probe is the target's Ensembl gene;
- ``study_design`` is ``quantitative``, ``tissue`` and ``sample_size`` are the
  ``--tissue`` and ``--sample-size`` the operator states (eQTLGen's
  study-wide N, the same on every probe), and nothing else is declared: no
  ancestry label (Assigned Ancestry is measured), no build (the release
  config's default) and no trait ontology term (a gene is not the Trait, #141).
  The tissue is recorded in its own column, never in the id, and a blank or
  padded one is refused before anything is written (issue #319).

A probe the table cannot describe -- a chromosome outside ``1``-``22``, ``X``,
``Y``, ``MT``, a repeated probe, a short row -- fails the whole adapter by line
and probe, and a table the canonical loader refuses is removed, so the adapter
never leaves one behind for freeze.

Usage:
    python3 -m manifest_generation.providers.eqtlgen.source_metadata_adapter \\
        --epi /data/opengwasdb/raw/eqtlgen/eqtlgen-sparse.epi \\
        --tissue whole_blood --sample-size 31684 --out <source-metadata.tsv>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from manifest_generation.connector_tsv import raise_problems, write_connector
from manifest_generation.source_inventory import InventoryError
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
    "tissue",
    "target_ensembl_gene_id",
    "target_chromosome",
    "target_start",
    "target_end",
)

#: ``.epi`` columns: chromosome, probe, genetic distance, base pair, gene, orientation.
EPI_COLUMNS = 6


class AdapterError(InventoryError):
    """The ``.epi`` cannot be converted into a valid Source Metadata Table."""


def convert_epi(epi: Path, out: Path, *, tissue: str, sample_size: int) -> None:
    """Convert ``epi`` into a canonical Source Metadata Table at ``out``, then load it back."""
    epi, out = Path(epi), Path(out)
    if not epi.is_file():
        raise AdapterError(f"eQTLGen .epi not found: {epi}")
    if not tissue.strip() or tissue != tissue.strip():
        raise AdapterError(f"tissue {tissue!r} is empty or padded")
    if type(sample_size) is not int or sample_size < 1:
        raise AdapterError(f"sample size {sample_size!r} is not a positive whole number")
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
            "source_trait_label": gene or probe,
            "study_design": "quantitative",
            "sample_size": str(sample_size),
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
    write_connector(out, SOURCE_METADATA_TABLE, OUTPUT_COLUMNS, rows)
    try:
        load_source_metadata_table(out)
    except InventoryError:
        out.unlink()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--epi", required=True, type=Path, help="the full file set's .epi")
    parser.add_argument("--tissue", required=True, help="the measured tissue, e.g. whole_blood")
    parser.add_argument("--sample-size", required=True, type=int, help="the study's total N")
    parser.add_argument("--out", required=True, type=Path, help="the Source Metadata Table to write")
    args = parser.parse_args(argv)
    try:
        convert_epi(args.epi, args.out, tissue=args.tissue, sample_size=args.sample_size)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Source Metadata Table {args.out} written from {args.epi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
