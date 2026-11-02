#!/usr/bin/env python3
"""Write the tiny pre-#348 panels, and their records, that the repair tests use.

A panel written by the shipped builder has three properties this reproduces,
and nothing else:

* ``EAF`` is the **other** allele's frequency: the builder treated plink2's
  REF-dosage mean as ALT dosage, so every row's EAF is one minus the frequency
  of the row's ``EA`` (#348);
* an ALID can name two source records (a deletion and an insertion at one
  position), and both rows are present, because the builder had no
  one-record-per-ALID rule when they were written;
* ``vectors`` is the right singular vectors of the standardised effect-allele
  dosages of **all** rows, and ``values`` is every squared singular value. The
  shipped eigenvectors are uniformly sign-flipped relative to the effect
  allele, which no artifact can show: ``V diag(values) V^T`` is the same matrix
  either way, and that (the LD) is what a block stores.

Eigenvector *row* i is TSV row i, the one tie between the files. The genotypes
are literals below, so the frequencies the tests assert on are hand-computable:
8 individuals, so every REF-dosage mean is a multiple of 1/16 and ``1 - f`` is
exact (which is what lets a MAF tie be a real tie).

Run from the repository root, writing the set to `FIXTURES_OUT` when it is set:

    python3 tests/ld-panel-repair/fixtures/generate_fixtures.py

The suite always sets `FIXTURES_OUT` to a directory of its own. Nothing is
committed: a `.npz` is a zip archive, so its members' timestamps differ on every
write and a committed set would leave the checkout dirty after every test run.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
from pathlib import Path

import numpy as np

HERE = Path(os.environ.get("FIXTURES_OUT") or Path(__file__).resolve().parent)
PANEL = HERE / "panel"
REFERENCE = HERE / "reference.tsv.gz"
RECORDS = HERE / "records.json"

GROUP = "United Kingdom"

# (pos, REF, ALT, REF dosages over the 8 individuals). plink2's export counts
# REF, which is also the record's allele1, as in tools/ld-panel/construct_block.
BLOCKS: dict[str, list[tuple[int, str, str, list[int]]]] = {
    # A duplicated ALID whose records differ in MAF: the G>GA insertion is the
    # common record (MAF .5) and wins over the GA>G deletion (MAF .375), so the
    # earlier row survives here.
    "22/100-200": [
        (100, "A", "G", [1, 0, 0, 0, 0, 0, 0, 0]),
        (101, "G", "GA", [2, 2, 1, 1, 1, 1, 0, 0]),
        (101, "GA", "G", [1, 1, 0, 0, 0, 0, 0, 0]),
        (102, "C", "T", [1, 1, 0, 0, 0, 0, 0, 0]),
        (103, "G", "A", [1, 1, 0, 0, 1, 1, 0, 0]),
        (104, "T", "C", [2, 2, 2, 2, 2, 1, 1, 0]),
    ],
    # A duplicated ALID whose records are an exact MAF tie (.375 each), so the
    # earlier row wins, as AMR's one-record rule has it.
    "22/300-400": [
        (300, "A", "G", [2, 2, 2, 2, 2, 2, 2, 1]),
        (301, "G", "GA", [2, 2, 1, 1, 0, 0, 0, 0]),
        (301, "GA", "G", [2, 1, 1, 1, 1, 0, 0, 0]),
        (302, "C", "T", [1, 0, 0, 0, 0, 0, 0, 0]),
        (303, "T", "C", [2, 1, 1, 2, 0, 1, 1, 0]),
    ],
    # No duplicated ALID, and written before the REF/ALT columns existed: only
    # the EAF flip applies, and the npz must come out untouched.
    "22/500-600": [
        (500, "A", "C", [1, 1, 0, 0, 0, 0, 0, 0]),
        (501, "G", "T", [2, 2, 2, 2, 2, 2, 1, 1]),
        (502, "A", "T", [1, 1, 1, 1, 0, 0, 1, 0]),
        (503, "C", "G", [2, 2, 2, 1, 1, 1, 1, 1]),
    ],
}

# The AMR panel carries its source record's alleles; AFR/EAS/SAS/EUR do not.
SOURCE_ALLELE_COLUMNS = {"22/100-200", "22/300-400"}

FIELDS = ["CHR", "BP", "SNP", "OA", "EA", "EAF", "MAC"]


def alid(chrom: str, pos: int, ref: str, alt: str) -> str:
    return f"{chrom}:{pos}:{min(ref, alt)}:{max(ref, alt)}"


def ea_frequency(dosages: list[int], ref: str, alt: str) -> float:
    """Frequency of the canonical effect allele (the smaller) from REF dosages."""
    mean = sum(dosages) / len(dosages)
    return mean / 2 if min(ref, alt) == ref else 1 - mean / 2


def standardised(dosages: list[int], ref: str, alt: str) -> np.ndarray:
    column = np.asarray(dosages, dtype=np.float64)
    if min(ref, alt) != ref: column = 2 - column
    column -= column.mean()
    return column / np.sqrt((column * column).sum())


def write_block(name: str, records: list[tuple[int, str, str, list[int]]]) -> None:
    chrom, span = name.split("/")
    rows, columns = [], []
    for pos, ref, alt, dosages in records:
        effect, other = min(ref, alt), max(ref, alt)
        frequency = ea_frequency(dosages, ref, alt)
        minor_allele_count = round(min(frequency, 1 - frequency) * 2 * len(dosages))
        row = {"CHR": chrom, "BP": pos, "SNP": alid(chrom, pos, ref, alt), "OA": other,
               "EA": effect, "EAF": 1 - frequency,  # the #348 inversion
               "MAC": float(minor_allele_count)}
        if name in SOURCE_ALLELE_COLUMNS: row |= {"REF": ref, "ALT": alt}
        rows.append(row); columns.append(standardised(dosages, ref, alt))
    x = np.column_stack(columns)
    _, singular, vt = np.linalg.svd(x, full_matrices=False)
    values, vectors = singular * singular, vt.T
    base = PANEL / "EUR" / chrom / span
    base.parent.mkdir(parents=True, exist_ok=True)
    with base.with_suffix(".tsv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS + (["REF", "ALT"] if name in SOURCE_ALLELE_COLUMNS else []),
                                delimiter="\t")
        writer.writeheader(); writer.writerows(rows)
    with base.with_suffix(".ldeig.npz").open("wb") as fh:
        np.savez_compressed(fh, values=values, vectors=vectors)
    provenance = {"interval": f"{chrom}:{span}", "n_individuals": len(records[0][3]),
                  "n_variants_input": len(rows), "n_variants_retained": len(rows),
                  "mac_threshold": 50, "maf_threshold": None, "max_missing_rate": None,
                  "cumulative_variance_target": .99, "component_floor": 2,
                  "components_retained": int(vectors.shape[1]), "achieved_variance": 1.0}
    base.with_suffix(".provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")


def surviving(records: list[tuple[int, str, str, list[int]]], chrom: str) -> list[tuple[int, str, str, list[int]]]:
    """The records the one-record-per-ALID rule keeps: highest MAF, ties to the earlier row."""
    best: dict[str, tuple[float, int]] = {}
    for i, (pos, ref, alt, dosages) in enumerate(records):
        frequency = ea_frequency(dosages, ref, alt)
        maf = min(frequency, 1 - frequency)
        key = alid(chrom, pos, ref, alt)
        if key not in best or maf > best[key][0]: best[key] = (maf, i)
    return [records[i] for _, i in sorted(best.values(), key=lambda pair: pair[1])]


def main() -> None:
    for name, records in BLOCKS.items(): write_block(name, records)
    document: dict[str, object] = {"individuals": 8, "group": GROUP, "blocks": {}}
    reference = ["\t".join(["alid", "chromosome", "position", "effect_allele", "other_allele", "rsid", GROUP])]
    for name, records in BLOCKS.items():
        chrom = name.split("/")[0]
        document["blocks"][name] = [
            {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt, "effect": min(ref, alt),
             "frequency": ea_frequency(dosages, ref, alt), "dosages": dosages}
            for pos, ref, alt, dosages in records]
        # The reference holds one frequency per ALID: the surviving record's
        # frequency of the panel's EA, which is what a correct panel reports.
        for pos, ref, alt, dosages in surviving(records, chrom):
            effect, other = min(ref, alt), max(ref, alt)
            reference.append("\t".join([alid(chrom, pos, ref, alt), chrom, str(pos), effect, other, ".",
                                        repr(ea_frequency(dosages, ref, alt))]))
    RECORDS.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    # mtime 0, so regenerating the fixtures leaves the file byte-identical: a
    # fixture that churns on every test run is a diff nobody can read.
    with REFERENCE.open("wb") as raw, gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0) as compressed, \
            io.TextIOWrapper(compressed, newline="") as fh:
        fh.write("\n".join(reference) + "\n")
    print(f"wrote {PANEL} and {REFERENCE.name}")


if __name__ == "__main__":
    main()
