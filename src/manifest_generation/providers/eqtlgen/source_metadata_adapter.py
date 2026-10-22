#!/usr/bin/env python3
"""eQTLGen metadata adapter: a BESD file set -> Source Metadata Table (#291, #314, ADR 0036).

eQTLGen's cis-eQTL summary statistics are one BESD file set holding an Analysis
per probe (an Ensembl gene). The ``.epi`` names every probe and its position and
carries no study metadata, so this adapter writes one canonical row per probe
(``manifest_generation.source_metadata_table``), takes the tissue from the
operator, never from a default, and derives each probe's sample size from the file:

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
- ``sample_size`` is the probe's maximum pair N (below), ``study_design`` is
  ``quantitative``, ``tissue`` is the ``--tissue`` the operator states, and
  nothing else is declared: no
  ancestry label (Assigned Ancestry is measured), no build (the release
  config's default) and no trait ontology term (a gene is not the Trait, #141).
  The tissue is recorded in its own column, never in the id, and a blank or
  padded one is refused before anything is written (issue #319).

**Sample size.** eQTLGen's beta and SE are z-standardised with each SNP-gene
pair's own N: b = z / sqrt(2p(1-p)(N + z^2)) and se = 1 / sqrt(2p(1-p)(N + z^2)).
So a pair's N is recoverable, ``N = 1 / (2p(1-p) se^2) - z^2`` with z = b / se and
p the ``.esi`` frequency, and the probe's ``sample_size`` is the maximum over its
pairs, rounded to the nearest integer (halves up) and capped at ``--study-total``.
Only pairs with finite N and ``|z| >= 1`` count. The formula is ill-conditioned
for small z because the stored values are coarse: against the published
per-pair ``NrSamples`` (12 genes, 85,079 pairs) the relative error of derived N
is up to 300% for ``|z|`` < 0.001, 0.4% below 0.5, 0.08% below 1 and at most
0.04% from 1; without the bound 61% of probes took an N above the study's 31,684.
The cap only absorbs that remaining rounding noise (at most 13 over). A probe with
no qualifying pair fails the adapter by name. Per-association N is not stored yet
(opengwasdb#302).

A probe the table cannot describe -- a chromosome outside ``1``-``22``, ``X``,
``Y``, ``MT``, a repeated probe, a short row -- fails the whole adapter by line
and probe, and a table the canonical loader refuses is removed, so the adapter
never leaves one behind for freeze.

Usage:
    python3 -m manifest_generation.providers.eqtlgen.source_metadata_adapter \\
        --prefix /data/opengwasdb/raw/eqtlgen/eqtlgen-sparse \\
        --tissue whole_blood --study-total 31684 --out <source-metadata.tsv>
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from pathlib import Path
from typing import Sequence

import numpy as np

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

#: A pair counts toward a probe's N only at ``|z|`` at least this: below it the stored
#: beta and SE are too coarse for the formula (see the module docstring).
MIN_ABS_Z = 1.0

#: The magic, 15 reserved uint32s and the uint64 ``val_num`` that open a SPARSE_FILE_TYPE_3 ``.besd``.
BESD_HEADER_BYTES = 4 + 15 * 4 + 8
#: The ``.esi`` column holding the SNP's allele frequency.
ESI_FREQUENCY_COLUMN = 6


class AdapterError(InventoryError):
    """The ``.epi`` cannot be converted into a valid Source Metadata Table."""


def convert(prefix: Path, out: Path, *, tissue: str, study_total: int) -> None:
    """Convert the file set at ``prefix`` into a canonical Source Metadata Table at ``out``, then load it back."""
    prefix, out = Path(prefix), Path(out)
    epi = Path(f"{prefix}.epi")
    if not epi.is_file():
        raise AdapterError(f"eQTLGen .epi not found: {epi}")
    if not tissue.strip() or tissue != tissue.strip():
        raise AdapterError(f"tissue {tissue!r} is empty or padded")
    if type(study_total) is not int or study_total < 1:
        raise AdapterError(f"study total {study_total!r} is not a positive whole number")
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
    for row, n in zip(rows, probe_sample_sizes(prefix, [row["source_probe_id"] for row in rows], study_total)):
        row["sample_size"] = str(n)
    write_connector(out, SOURCE_METADATA_TABLE, OUTPUT_COLUMNS, rows)
    try:
        load_source_metadata_table(out)
    except InventoryError:
        out.unlink()
        raise


def probe_sample_sizes(prefix: Path, probes: Sequence[str], study_total: int) -> list[int]:
    """Each probe's maximum pair N: rounded, over pairs with finite N and ``|z|`` >= 1, capped at ``study_total``.

    ``probes`` is the ``.epi`` in file order, whose line is the probe's index in the
    ``.besd``. A probe with no qualifying pair is refused by name.
    """
    besd, esi = Path(f"{prefix}.besd"), Path(f"{prefix}.esi")
    for member in (besd, esi):
        if not member.is_file():
            raise AdapterError(f"eQTLGen BESD member not found: {member}")
    frequency = np.array(
        [float(line.split("\t")[ESI_FREQUENCY_COLUMN]) for line in esi.read_text(encoding="utf-8").splitlines() if line.strip()]
    )
    n_probes = len(probes)
    with open(besd, "rb") as fh:
        header = fh.read(BESD_HEADER_BYTES)
    if len(header) < BESD_HEADER_BYTES or struct.unpack("<I", header[:4])[0] != 3:
        raise AdapterError(f"{besd} is not a SPARSE_FILE_TYPE_3 BESD file")
    val_num = struct.unpack("<Q", header[-8:])[0]
    cols_bytes = 8 * (2 * n_probes + 1)
    expected = BESD_HEADER_BYTES + cols_bytes + 8 * val_num
    if besd.stat().st_size != expected:
        raise AdapterError(
            f"{besd} has {besd.stat().st_size} bytes, not the {expected} its header and {n_probes} probes imply"
        )
    raw = np.memmap(besd, dtype=np.uint8, mode="r")
    cols = np.frombuffer(raw[BESD_HEADER_BYTES:BESD_HEADER_BYTES + cols_bytes], dtype="<i8")
    start = BESD_HEADER_BYTES + cols_bytes
    rowid = np.frombuffer(raw[start:start + 4 * val_num], dtype="<u4")
    val = np.frombuffer(raw[start + 4 * val_num:start + 8 * val_num], dtype="<f4")
    sizes: list[int] = []
    no_pair: list[str] = []
    for index, probe in enumerate(probes):
        first, middle = int(cols[2 * index]), int(cols[2 * index + 1])
        count = middle - first
        beta = val[first:middle].astype(np.float64)
        se = val[middle:middle + count].astype(np.float64)
        p = frequency[rowid[first:middle]]
        with np.errstate(divide="ignore", invalid="ignore"):
            z = beta / se
            n_pair = 1.0 / (2.0 * p * (1.0 - p) * se**2) - z**2
        usable = np.isfinite(n_pair) & (np.abs(z) >= MIN_ABS_Z)
        if not usable.any():
            no_pair.append(probe)
            sizes.append(0)
            continue
        sizes.append(min(int(math.floor(float(n_pair[usable].max()) + 0.5)), study_total))
    if no_pair:
        raise_problems(
            AdapterError, f"eQTLGen BESD {besd}",
            [f"{probe}: no pair with finite N and |z| >= {MIN_ABS_Z:g}" for probe in no_pair],
        )
    return sizes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--prefix", required=True, type=Path, help="the BESD file set's prefix (.besd, .epi, .esi)")
    parser.add_argument("--tissue", required=True, help="the measured tissue, e.g. whole_blood")
    parser.add_argument("--study-total", required=True, type=int, help="the study's total N, the cap on a probe's N")
    parser.add_argument("--out", required=True, type=Path, help="the Source Metadata Table to write")
    args = parser.parse_args(argv)
    try:
        convert(args.prefix, args.out, tissue=args.tissue, study_total=args.study_total)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Source Metadata Table {args.out} written from {args.prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
