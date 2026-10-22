#!/usr/bin/env python3
"""The eQTLGen gene-symbol map: probe (Ensembl gene) -> GeneSymbol (#314).

eQTLGen's BESD ``.epi`` names no gene symbol, but its published cis-eQTL file
carries ``Gene`` and ``GeneSymbol`` on every row. This streams that file once
(gzipped, over a URL or ``file://``) and writes the symbols of the probes asked
for as a small connector input, ``gene<TAB>gene_symbol``, with its lineage in
``<out>.meta.yaml``: the URL, the downloaded file's sha256 and byte count (of the
compressed stream), and the download date. The metadata adapter uses the map for
the Analysis Label only; ``analysis_id`` and ``source_probe_id`` stay the bare
Ensembl ids.

A gene the file gives two symbols is refused by name and nothing is written. A
probe with a blank symbol, or none in the file, is left out of the map (its label
stays its Ensembl id) and counted.

Usage:
    python3 -m manifest_generation.providers.eqtlgen.gene_symbols \\
        --url https://molgenis26.gcc.rug.nl/downloads/eqtlgen/cis-eqtl/2019-12-11-cis-eQTLsFDR-ProbeLevel-CohortInfoRemoved-BonferroniAdded.txt.gz \\
        --epi <file set>.epi --out resources/generators/eqtlgen-cis/connectors/gene-symbols.tsv
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import io
import sys
import urllib.request
from pathlib import Path
from typing import Sequence

import yaml

from manifest_generation.connector_tsv import raise_problems
from manifest_generation.source_inventory import InventoryError, atomic_write

#: The published file's columns this reads.
GENE_COLUMN = "Gene"
SYMBOL_COLUMN = "GeneSymbol"


class GeneSymbolError(InventoryError):
    """The published file cannot give an unambiguous symbol map."""


class _Counted(io.RawIOBase):
    """A read-only stream that hashes and counts the bytes passing through it."""

    def __init__(self, raw) -> None:
        self._raw = raw
        self.sha256 = hashlib.sha256()
        self.bytes = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        chunk = self._raw.read(len(buffer))
        buffer[: len(chunk)] = chunk
        self.sha256.update(chunk)
        self.bytes += len(chunk)
        return len(chunk)


def build_map(url: str, probes: Sequence[str], out: Path, *, today: str | None = None) -> dict[str, int]:
    """Write the symbol map of ``probes`` to ``out`` and its lineage beside it; return the counts."""
    out = Path(out)
    wanted = set(probes)
    symbols: dict[str, set[str]] = {}
    with urllib.request.urlopen(url) as response:  # noqa: S310 - the caller names the URL
        counted = _Counted(response)
        text = io.TextIOWrapper(gzip.GzipFile(fileobj=io.BufferedReader(counted)), encoding="utf-8", newline="")
        header = text.readline().rstrip("\r\n").split("\t")
        if GENE_COLUMN not in header or SYMBOL_COLUMN not in header:
            raise GeneSymbolError(f"{url}: no {GENE_COLUMN} and {SYMBOL_COLUMN} columns in {header[:20]}")
        gene_at, symbol_at = header.index(GENE_COLUMN), header.index(SYMBOL_COLUMN)
        for line in text:
            fields = line.rstrip("\r\n").split("\t")
            gene = fields[gene_at]
            if gene not in wanted:
                continue
            symbol = fields[symbol_at].strip() if symbol_at < len(fields) else ""
            symbols.setdefault(gene, set())
            if symbol:
                symbols[gene].add(symbol)
        for _ in iter(lambda: counted.read(1 << 20), b""):  # drain to the end so the digest covers the whole file
            pass
    conflicts = {gene: names for gene, names in symbols.items() if len(names) > 1}
    if conflicts:
        raise_problems(
            GeneSymbolError, f"published file {url}",
            [f"{gene}: conflicting symbols {', '.join(sorted(names))}" for gene, names in sorted(conflicts.items())],
        )
    mapped = {gene: next(iter(names)) for gene, names in symbols.items() if names}
    atomic_write(out, "gene\tgene_symbol\n" + "".join(f"{g}\t{mapped[g]}\n" for g in sorted(mapped)))
    counts = {
        "probes": len(wanted),
        "with_symbol": len(mapped),
        "without_symbol": len(wanted) - len(mapped),
    }
    atomic_write(
        Path(f"{out}.meta.yaml"),
        yaml.safe_dump(
            {
                "url": url,
                "sha256": counted.sha256.hexdigest(),
                "bytes": counted.bytes,
                "downloaded": today or datetime.date.today().isoformat(),
                "written_by": __spec__.name if __spec__ else "gene_symbols",
                **counts,
            },
            sort_keys=False,
        ),
    )
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="eQTLGen's published cis-eQTL file (gzipped, tab separated)")
    parser.add_argument("--epi", required=True, type=Path, help="the file set's .epi: the probes to map")
    parser.add_argument("--out", required=True, type=Path, help="the gene-symbol connector input to write")
    args = parser.parse_args(argv)
    try:
        probes = [line.split("\t")[1] for line in args.epi.read_text(encoding="utf-8").splitlines() if line.strip()]
        counts = build_map(args.url, probes, args.out)
    except (InventoryError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Gene symbols {args.out}: {counts['with_symbol']} of {counts['probes']} probes have one; "
          f"{counts['without_symbol']} keep their Ensembl id")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
