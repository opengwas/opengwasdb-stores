#!/usr/bin/env python3
"""Compare a built Store's indel ALIDs with a raw GWAS-SSF file's reference-first spelling (#455).

For a newer-layout GWAS-SSF file (harmonised alleles in the main columns, `hm_code` beside
them) the engine (opengwasdb#381, ADR 0079 section 7) reads `other_allele` as the reference of
a code-10 or code-11 row and spells that indel's ALID reference first:
`chromosome:position:other_allele:effect_allele`. This reads the RAW file, forms that spelling
itself (not through the engine's `canonical_alid`), and compares it with the indel ALIDs of a
Store built from the file under test (the raw file, or a rescued copy of it).

  pixi run -e dev python3 tools/check_indel_alid_spelling.py RAW.h.tsv.gz STORE.opengwasdb \
      --label regen --json out.json

Reads the Store's variants.tsv.gz, so it imports nothing from the engine; exits 1 when any raw indel is not
spelt reference-first in the Store, or the Store holds an indel the raw file does not.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path


ACGT = frozenset("ACGT")


def raw_indels(raw: Path) -> tuple[set[str], set[str], int]:
    """(reference-first ALIDs, their inverted spellings, indel rows read) of code-10/11 rows."""
    ref_first: set[str] = set()
    inverted: set[str] = set()
    rows = 0
    with gzip.open(raw, "rt", encoding="utf-8") as fh:
        hdr = fh.readline().rstrip("\r\n").split("\t")
        c, p, ea, oa, hm = (hdr.index(n) for n in ("chromosome", "base_pair_location", "effect_allele", "other_allele", "hm_code"))
        for line in fh:
            f = line.rstrip("\r\n").split("\t")
            e, o = f[ea].upper(), f[oa].upper()
            if len(e) == len(o) or not (ACGT >= set(e) and ACGT >= set(o)) or f[hm] not in ("10", "11"):
                continue
            rows += 1
            ref_first.add(f"{f[c]}:{f[p]}:{o}:{e}")
            inverted.add(f"{f[c]}:{f[p]}:{e}:{o}")
    return ref_first, inverted, rows


def store_indels(store: Path) -> set[str]:
    """The indel ALIDs of a Store's `variants.tsv.gz` (BGZF, so `gzip` reads it).

    Columns: chromosome, position, variant_index, effect_allele, other_allele, alid, ...;
    `#` lines are the header (as `src/ogstores/audit.py` reads it).
    """
    out: set[str] = set()
    with gzip.open(store / "variants.tsv.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            alid = line.split("\t")[5]
            _, _, a1, a2 = alid.split(":")
            if len(a1) != len(a2):
                out.add(alid)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("raw", type=Path)
    ap.add_argument("store", type=Path)
    ap.add_argument("--label", default="")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    expected, inverted, rows = raw_indels(a.raw)
    built = store_indels(a.store)
    # An indel spelt both ways in the raw file (a row each way) cannot tell the two apart.
    both = expected & inverted
    result = {
        "label": a.label,
        "raw_indel_rows_code_10_11": rows,
        "expected_reference_first_alids": len(expected),
        "store_indel_alids": len(built),
        "expected_spelt_reference_first_in_store": len(expected & built),
        "expected_missing_from_store": len(expected - built),
        "store_indels_not_in_raw_expectation": len(built - expected),
        "expected_found_only_inverted": len((inverted - expected) & built),
        "alids_spelt_both_ways_in_raw": len(both),
        "examples_missing": sorted(expected - built)[:5],
        "examples_unexpected": sorted(built - expected)[:5],
    }
    print(json.dumps(result, indent=2))
    if a.json:
        a.json.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if not (expected - built) and not (built - expected) else 1


if __name__ == "__main__":
    sys.exit(main())
