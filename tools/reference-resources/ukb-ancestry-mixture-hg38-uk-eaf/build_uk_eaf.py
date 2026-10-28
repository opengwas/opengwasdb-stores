#!/usr/bin/env python3
"""Derive the UK Biobank "United Kingdom" EAF table from ukb-ancestry-mixture-hg38 (#282).

``opengwasdb build-ragged-besd --eaf-reference`` takes a table with an ``alid``
column and an ``eaf`` column and checks a source's stored allele frequencies against
it (ADR 0067). ukb-ancestry-mixture-hg38 holds one column per fine ancestry group,
each already oriented to the canonical A1 of its ``alid`` (chr:pos:A1:A2), so the
"United Kingdom" column is that table's ``eaf`` as it stands: this copies it, rows
and order unchanged. It never reads an hgdp1kgp-hg38 panel, whose EAF column is the
other allele's frequency (#348).

Writes ``--out`` (gzipped TSV, ``alid`` and ``eaf``) and ``<out>.meta.yaml``: the
source's path, sha256 and byte count, the column, and the row counts. Never overwrites.

Usage:
  pixi run python tools/reference-resources/ukb-ancestry-mixture-hg38-uk-eaf/build_uk_eaf.py \\
    --out /data/opengwasdb/reference/ancestry-mixture/ref_eaf.united-kingdom.hg38.tsv.gz
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import sys
from pathlib import Path

import yaml

DEFAULT_FREQS = "/data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz"
DEFAULT_COLUMN = "United Kingdom"


def build(freqs: Path, out: Path, column: str) -> dict:
    meta = Path(f"{out}.meta.yaml")
    for target in (out, meta):
        if target.exists():
            raise SystemExit(f"ERROR: {target} exists; this writes new files only")
    digest = hashlib.sha256()
    with open(freqs, "rb") as raw:
        for block in iter(lambda: raw.read(1 << 24), b""):
            digest.update(block)
    n_rows = n_blank = 0
    temporary = out.with_name(f".{out.name}.tmp")
    try:
        with gzip.open(freqs, "rt", encoding="utf-8", newline="") as source, gzip.open(
            temporary, "wt", encoding="utf-8", newline=""
        ) as table:
            header = source.readline().rstrip("\r\n").split("\t")
            for needed in ("alid", column):
                if needed not in header:
                    raise SystemExit(f"ERROR: {freqs} has no {needed!r} column in {header[:8]}")
            alid_at, column_at = header.index("alid"), header.index(column)
            table.write("alid\teaf\n")
            for line in source:
                fields = line.rstrip("\r\n").split("\t")
                n_rows += 1
                value = fields[column_at]
                if not value or value == "NA":
                    n_blank += 1
                    continue
                table.write(f"{fields[alid_at]}\t{value}\n")
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)
    lineage = {
        "source_resource_id": "ukb-ancestry-mixture-hg38",
        "source_path": str(freqs),
        "source_sha256": digest.hexdigest(),
        "source_bytes": Path(freqs).stat().st_size,
        "column": column,
        "written_by": "tools/reference-resources/ukb-ancestry-mixture-hg38-uk-eaf/build_uk_eaf.py",
        "rows_read": n_rows,
        "rows_blank": n_blank,
        "rows_written": n_rows - n_blank,
        "table_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "table_bytes": out.stat().st_size,
    }
    meta.write_text(yaml.safe_dump(lineage, sort_keys=False), encoding="utf-8")
    return lineage


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--freqs", default=DEFAULT_FREQS, type=Path)
    parser.add_argument("--column", default=DEFAULT_COLUMN)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    lineage = build(args.freqs, args.out, args.column)
    print(f"{args.out}: {lineage['rows_written']} of {lineage['rows_read']} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
