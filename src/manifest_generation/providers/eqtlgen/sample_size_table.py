#!/usr/bin/env python3
"""The eQTLGen per-pair sample-size table: published file + BESD -> table (#302, ADR 0067).

opengwasdb's ``build-ragged-besd --sample-size-table`` stores a per-association
``n`` from a tab-separated table of ``probe_id``, ``snp_id`` and ``n``, grouped by
probe in ``.epi`` order. eQTLGen publishes the N of every (gene, SNP) pair as
``NrSamples`` in its cis-eQTL file, ordered by neither. This streams the published
file once and writes the table **aligned to a BESD file set**: one row for every
(probe, SNP) pair the BESD holds that the published file also holds, probes in
``.epi`` order, pairs in the BESD's own order within a probe. The N is the published
integer, never an estimate: a BESD pair the published file lacks gets no row (the
build leaves it missing), and a published pair the BESD lacks is counted and dropped.

A pair is keyed by the ``.epi`` probe id and the ``.esi`` SNP id, which the published
file names ``Gene`` and ``SNP``. A (gene, SNP) published twice among the pairs the
file set holds, an ``.esi`` SNP id reached twice by one probe, and an ``NrSamples``
that is not a whole number from 1 are refused by name, and nothing is written.

The table's lineage is written beside it as ``<table>.provenance.yaml``: the published
file's URL, sha256 and byte count, the BESD members' sha256s, the table's own sha256
and the counts (BESD pairs, published rows, matched, unmatched, published-only).

Usage:
    python3 -m manifest_generation.providers.eqtlgen.sample_size_table \\
        --published <cis-eQTL file .txt.gz> --source-url <its URL> \\
        --prefix /data/opengwasdb/raw/eqtlgen/full-19250 \\
        --out /data/opengwasdb/raw/eqtlgen/full-19250.sample-size.tsv.gz
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import struct
import sys
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
import yaml
from opengwasdb.layouts.ragged.besd_reader import BESDReader

from manifest_generation.source_inventory import InventoryError, atomic_write

#: The published file's columns this reads.
GENE_COLUMN, SNP_COLUMN, N_COLUMN = "Gene", "SNP", "NrSamples"
#: The table's columns, in order.
TABLE_COLUMNS = ("probe_id", "snp_id", "n")
#: The largest N the store holds (``N_MISSING`` is the next code).
N_MAX = 2**32 - 2
#: Published rows read at a time.
CHUNK_ROWS = 5_000_000
#: The key of a pair: probe index in the high word, SNP name index in the low.
KEY_SHIFT = 32


#: The magic, 15 reserved uint32s and the uint64 ``val_num`` that open a SPARSE_FILE_TYPE_3 ``.besd``.
BESD_HEADER_BYTES = 4 + 15 * 4 + 8


class SampleSizeTableError(InventoryError):
    """The published file and BESD cannot give an unambiguous sample-size table."""


def check_besd_layout(besd: Path, n_probes: int) -> None:
    """Refuse a ``.besd`` whose length or column-offset table does not describe ``n_probes`` probes.

    ``BESDReader`` trusts the offsets it reads, so a damaged table would read as
    plausible but wrong associations. The file is SPARSE_FILE_TYPE_3 and its size is
    the one its header and probe count imply; the first offset is 0, the last is the
    file's value count, offsets never decrease, and each probe's beta block and SE
    block are the same length.
    """
    with open(besd, "rb") as fh:
        header = fh.read(BESD_HEADER_BYTES)
        if len(header) < BESD_HEADER_BYTES or struct.unpack("<I", header[:4])[0] != 3:
            raise SampleSizeTableError(f"{besd} is not a SPARSE_FILE_TYPE_3 BESD file")
        val_num = struct.unpack("<Q", header[-8:])[0]
        cols_bytes = 8 * (2 * n_probes + 1)
        expected = BESD_HEADER_BYTES + cols_bytes + 8 * val_num
        if besd.stat().st_size != expected:
            raise SampleSizeTableError(
                f"{besd} has {besd.stat().st_size} bytes, not the {expected} its header and {n_probes} probes imply"
            )
        cols = np.frombuffer(fh.read(cols_bytes), dtype="<i8")
    problems: list[str] = []
    if int(cols[0]) != 0:
        problems.append(f"the first offset is {int(cols[0])}, not 0")
    if int(cols[-1]) != val_num:
        problems.append(f"the final offset is {int(cols[-1])}, not the file's {val_num} values")
    steps = np.diff(cols)
    if (steps < 0).any():
        first = int(np.argmax(steps < 0))
        problems.append(
            f"offsets do not increase: offset {first + 1} ({int(cols[first + 1])}) is below offset {first} ({int(cols[first])})"
        )
    elif not (steps[0::2] == steps[1::2]).all():
        bad = int(np.argmax(steps[0::2] != steps[1::2]))
        problems.append(
            f"probe {bad} has a beta block of {int(steps[2 * bad])} values and an SE block of {int(steps[2 * bad + 1])}"
        )
    if problems:
        raise SampleSizeTableError(f"{besd}: damaged column offsets: {'; '.join(problems)}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 24), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_epi(path: Path) -> list[str]:
    return [line.split("\t")[1] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_esi(path: Path) -> list[str]:
    return [line.split("\t", 2)[1] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _published_pairs(published: Path, probes: pd.Index, snps: pd.Index) -> tuple[np.ndarray, np.ndarray, int]:
    """Sorted keys and N of the published pairs the file set could hold, and the row count read."""
    keys: list[np.ndarray] = []
    sizes: list[np.ndarray] = []
    n_rows = 0
    with pd.read_csv(
        published, sep="\t", usecols=[GENE_COLUMN, SNP_COLUMN, N_COLUMN],
        dtype={GENE_COLUMN: str, SNP_COLUMN: str, N_COLUMN: np.float64},
        na_values=["NA", ""], keep_default_na=False, chunksize=CHUNK_ROWS, compression="gzip",
    ) as reader:
        for chunk in reader:
            n_rows += len(chunk)
            n = chunk[N_COLUMN].to_numpy()
            with np.errstate(invalid="ignore"):
                bad = ~(np.isfinite(n) & (n == np.floor(n)) & (n >= 1) & (n <= N_MAX))
            if bad.any():
                first = chunk[bad].iloc[0]
                raise SampleSizeTableError(
                    f"{published}: {first[GENE_COLUMN]} {first[SNP_COLUMN]}: {N_COLUMN} {first[N_COLUMN]!r} "
                    f"is not a whole number from 1 ({int(bad.sum())} such rows in rows ending at {n_rows})"
                )
            probe_at = probes.get_indexer(chunk[GENE_COLUMN])
            snp_at = snps.get_indexer(chunk[SNP_COLUMN])
            held = (probe_at >= 0) & (snp_at >= 0)
            keys.append((probe_at[held].astype(np.uint64) << KEY_SHIFT) | snp_at[held].astype(np.uint64))
            sizes.append(n[held].astype(np.uint32))
    all_keys = np.concatenate(keys) if keys else np.empty(0, dtype=np.uint64)
    all_sizes = np.concatenate(sizes) if sizes else np.empty(0, dtype=np.uint32)
    order = np.argsort(all_keys, kind="stable")
    all_keys, all_sizes = all_keys[order], all_sizes[order]
    repeated = np.flatnonzero(all_keys[1:] == all_keys[:-1])
    if repeated.size:
        key = int(all_keys[repeated[0]])
        raise SampleSizeTableError(
            f"{published}: {probes[key >> KEY_SHIFT]} {snps[key & 0xFFFFFFFF]}: published more than once "
            f"({repeated.size} repeated pairs among those the file set holds)"
        )
    return all_keys, all_sizes, n_rows


def build_table(published: Path, prefix: Path, out: Path, *, source_url: str) -> dict:
    """Write the table of ``prefix``'s pairs and its lineage beside ``out``; return the lineage."""
    published, prefix, out = Path(published), Path(prefix), Path(out)
    members = {suffix: Path(f"{prefix}.{suffix}") for suffix in ("besd", "epi", "esi")}
    for member in [published, *members.values()]:
        if not member.is_file():
            raise SampleSizeTableError(f"not found: {member}")
    probe_ids = _read_epi(members["epi"])
    snp_rows = _read_esi(members["esi"])
    check_besd_layout(members["besd"], len(probe_ids))
    probes = pd.Index(probe_ids)
    if not probes.is_unique:
        raise SampleSizeTableError(f"{members['epi']}: a probe is listed twice")
    row_code, unique_snps = pd.factorize(pd.Index(snp_rows))
    row_code = row_code.astype(np.uint64)
    snp_names = np.asarray(snp_rows, dtype=object)

    with open(published, "rb") as fh:
        header = gzip.GzipFile(fileobj=fh).readline().decode("utf-8").rstrip("\r\n").split("\t")
    missing = [c for c in (GENE_COLUMN, SNP_COLUMN, N_COLUMN) if c not in header]
    if missing:
        raise SampleSizeTableError(f"{published}: no {', '.join(missing)} column in {header[:20]}")
    keys, sizes, n_published = _published_pairs(published, probes, pd.Index(unique_snps))

    reader = BESDReader(members["besd"], len(probe_ids))
    counts = {"besd_pairs": 0, "published_rows": n_published, "matched": 0, "unmatched": 0, "published_only": 0}
    temporary = out.with_name(f".{out.name}.tmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", newline="") as table:
            table.write("\t".join(TABLE_COLUMNS) + "\n")
            for index, probe in enumerate(probe_ids):
                rows, _beta, _se = reader.get_probe_associations(index)
                codes = row_code[rows]
                if np.unique(codes).size != codes.size:
                    names, counted = np.unique(snp_names[rows], return_counts=True)
                    raise SampleSizeTableError(
                        f"{members['esi']}: {probe} reaches SNP id {names[counted > 1][0]} on more than one .esi row"
                    )
                want = (np.uint64(index) << np.uint64(KEY_SHIFT)) | codes
                at = np.searchsorted(keys, want)
                at[at == keys.size] = max(keys.size - 1, 0)
                found = keys[at] == want if keys.size else np.zeros(want.size, dtype=bool)
                counts["besd_pairs"] += int(want.size)
                counts["matched"] += int(found.sum())
                counts["unmatched"] += int((~found).sum())
                table.write(
                    "".join(f"{probe}\t{snp}\t{n}\n" for snp, n in zip(snp_names[rows][found], sizes[at[found]].tolist()))
                )
        counts["published_only"] = n_published - counts["matched"]
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)

    lineage = {
        "written_by": __spec__.name if __spec__ else "sample_size_table",
        "source_url": source_url,
        "source_sha256": _sha256(published),
        "source_bytes": published.stat().st_size,
        "besd": {suffix: _sha256(path) for suffix, path in members.items()},
        "table_sha256": _sha256(out),
        "table_bytes": out.stat().st_size,
        "counts": counts,
    }
    atomic_write(Path(f"{out}.provenance.yaml"), yaml.safe_dump(lineage, sort_keys=False, allow_unicode=True))
    return lineage


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--published", required=True, type=Path, help="eQTLGen's published cis-eQTL file (gzipped TSV)")
    parser.add_argument("--source-url", required=True, help="the URL the published file was downloaded from")
    parser.add_argument("--prefix", required=True, type=Path, help="the BESD file set's prefix (.besd, .epi, .esi)")
    parser.add_argument("--out", required=True, type=Path, help="the table to write (.tsv.gz); never overwrites")
    args = parser.parse_args(argv)
    for target in (args.out, Path(f"{args.out}.provenance.yaml")):
        if target.exists():
            print(f"ERROR: {target} exists; this writes new files only", file=sys.stderr)
            return 1
    try:
        lineage = build_table(args.published, args.prefix, args.out, source_url=args.source_url)
    except (InventoryError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Sample-size table {args.out}: {lineage['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
