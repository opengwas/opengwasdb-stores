#!/usr/bin/env python3
"""Check a variant axis actually covers the sources a release will build against.

An axis is only useful if the variants the sources report resolve against it.
This measures that directly on a sample of the release's own source files, and
compares two axes on the same sample so a replacement can be judged against the
thing it replaces rather than on its variant count alone.

    pixi run python resources/reference-resources/gnomad-v4-common-hg38/verify_axis_coverage.py \
      --inventory resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-22.tsv \
      --axis /data/opengwasdb/reference/gnomad-v4-common-hg38/EUR-variants.txt.gz \
      --axis /data/opengwasdb/reference/alid-panel/EUR-variants.tsv.gz \
      --sample 60
"""

from __future__ import annotations

import argparse
import gzip
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from resources.generators.lib.source_inventory import read_inventory  # noqa: E402

AUTOSOMES = {str(i) for i in range(1, 23)}


def load_axis(path: Path) -> set[str]:
    """Read any accepted variant-reference form as a set of canonical ALIDs."""
    from opengwasdb.variants.reference import read_variant_reference

    return set(read_variant_reference(path).alids)


def source_alids(path: Path, limit_rows: int) -> tuple[set[str], set[str]]:
    """A source's reportable canonical ALIDs, and which chromosomes it touched."""
    from opengwasdb.readers.gwas_ssf import GwasSsfReader

    reader = GwasSsfReader(path)
    alids: set[str] = set()
    chroms: set[str] = set()
    for row in reader.stream_metrics():
        alids.add(row.alid)
        chroms.add(row.alid.split(":", 1)[0])
        if len(alids) >= limit_rows:
            break
    return alids, chroms


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--inventory", type=Path, required=True)
    p.add_argument("--axis", type=Path, action="append", required=True,
                   help="repeatable; each axis is measured on the same sample")
    p.add_argument("--sample", type=int, default=60)
    p.add_argument("--rows-per-source", type=int, default=200_000)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    rows = [r for r in read_inventory(args.inventory) if r.ready and r.data_file]
    random.seed(args.seed)
    rows = random.sample(rows, min(args.sample, len(rows)))

    axes = {a: load_axis(a) for a in args.axis}
    for a, alids in axes.items():
        print(f"axis {a.name}: {len(alids):,} ALIDs", flush=True)

    hits = {a: 0 for a in axes}
    total = 0
    per_chrom: dict[str, int] = {}

    for i, row in enumerate(rows, 1):
        path = Path(row.data_file)
        if not path.exists():
            continue
        try:
            alids, chroms = source_alids(path, args.rows_per_source)
        except Exception as exc:  # noqa: BLE001 - coverage sample, not a gate
            print(f"  skip {row.analysis_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        total += len(alids)
        for chrom in chroms:
            if chrom not in AUTOSOMES:
                per_chrom[chrom] = per_chrom.get(chrom, 0) + 1
        for a, axis in axes.items():
            hits[a] += len(alids & axis)
        if i % 10 == 0:
            print(f"  {i}/{len(rows)} sources scanned", flush=True)

    print()
    for a in axes:
        print(f"{a.name:<52} {hits[a]/max(total,1):>8.2%} of source variants resolvable")
    if per_chrom:
        print("\nnon-autosomal sources in sample:", dict(sorted(per_chrom.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
