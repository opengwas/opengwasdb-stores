#!/usr/bin/env python3
"""Write a standalone SPARSE_FILE_TYPE_3 BESD triple for an analyses manifest.

Command-line interface::

    subset_besd.py ANALYSES_TSV INPUT_PREFIX OUTPUT_PREFIX

``analysis_id`` supplies the probe ID; an optional ``::tissue`` suffix is
removed. Rows whose ``exclude_from_build`` value is true are ignored.

`opengwasdb.layouts.ragged.besd_reader` (`BESDReader`/`read_esi`/`read_epi`)
already reads this format correctly and `build_ragged_from_besd` already
builds a real Ragged Store from it -- this module is the missing *write*
side needed to shrink a genome-wide eQTL BESD source (e.g. eQTLGen, ~19k
probes / ~9M SNPs / ~2GB) down to a small, real pilot subset before handing
it to that existing builder unmodified, mirroring how `gwas-ssf-ragged`'s
download+filter step shrinks a full harmonised file before opengwasdb ever
sees it. This keeps the BESD pilot "registry-side only" (issue #67's own
framing): opengwasdb's reader/builder are used as-is, on a smaller but
still-real, still-format-valid input.

Row indices in a BESD triple are positional (the Nth line of the .epi/.esi
files, 0-indexed) and the .besd file's offset table is keyed by that
position, so a probe subset can't just delete rows from a copy of the
original .epi file -- every SNP a kept probe references needs fresh, compact
renumbering, matched by a rewritten .esi containing only referenced SNPs.

## SPARSE_FILE_TYPE_3 layout (verified directly against a real eQTLGen file,
not assumed from documentation)

4-byte magic (``3``), 15 reserved uint32s, 8-byte ``val_num`` (uint64), then
a ``2*n_probes+1``-entry int64 cumulative-offset table (``cols``), a
``val_num``-entry uint32 SNP-index table (``rowid``), and a ``val_num``-entry
float32 value table (``val``). For probe ``i``: ``beta_start = cols[2i]``,
``se_start = cols[2i+1]``, ``n = se_start - beta_start`` associations, with
betas at ``val[beta_start:se_start]`` and SEs at ``val[se_start:se_start+n]``
-- i.e. each probe's beta block is immediately followed by its own SE block,
probes packed back-to-back (``cols[-1] == val_num``). ``rowid`` is written at
the same ``val_num`` length as ``val``, but only its first (beta-position)
half per probe is ever read; verified against real data that the second
(SE-position) half is a literal duplicate of the first, not padding -- this
writer reproduces that duplication rather than leaving it as an assumption.
"""

from __future__ import annotations

import argparse
import csv
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from opengwasdb.layouts.ragged.besd_reader import BESDReader, ProbeRecord, read_epi, read_esi

_MAGIC_SPARSE_3 = 3
_RESERVED_UNITS = 16


@dataclass(frozen=True)
class BesdSubsetResult:
    n_probes: int
    n_snps: int
    n_associations: int


def probe_ids_from_analyses(analyses_path: str | Path) -> list[str]:
    """Return buildable BESD probe IDs in ``analyses.tsv`` row order."""
    analyses_path = Path(analyses_path)
    with analyses_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames or "analysis_id" not in reader.fieldnames:
            raise ValueError(f"{analyses_path} is missing required column 'analysis_id'")
        probe_ids: list[str] = []
        seen: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            excluded = str(row.get("exclude_from_build") or "").strip().lower()
            if excluded in {"true", "1", "yes"}:
                continue
            analysis_id = str(row.get("analysis_id") or "").strip()
            if not analysis_id:
                raise ValueError(f"{analyses_path}:{line_number}: analysis_id is empty")
            probe_id = analysis_id.split("::", 1)[0]
            if probe_id in seen:
                raise ValueError(
                    f"{analyses_path}:{line_number}: duplicate BESD probe id {probe_id!r}"
                )
            seen.add(probe_id)
            probe_ids.append(probe_id)
    if not probe_ids:
        raise ValueError(f"{analyses_path} contains no buildable probes")
    return probe_ids


def write_besd_subset_from_analyses(
    analyses_path: str | Path,
    source_prefix: str | Path,
    dest_prefix: str | Path,
) -> BesdSubsetResult:
    """Write a BESD subset containing the probes declared by ``analyses.tsv``."""
    return write_besd_subset(
        source_prefix,
        dest_prefix,
        probe_ids=probe_ids_from_analyses(analyses_path),
    )


def write_besd_subset(
    source_prefix: str | Path, dest_prefix: str | Path, probe_ids: list[str]
) -> BesdSubsetResult:
    """Write ``{dest_prefix}.esi/.epi/.besd`` covering only ``probe_ids``.

    ``probe_ids`` must all exist in the source ``.epi``; the destination
    keeps their order. SNPs referenced by no selected probe are dropped from
    the new ``.esi`` entirely -- the subset is a real, independently valid
    BESD triple, not a view onto the original files.
    """
    source_prefix = Path(source_prefix)
    dest_prefix = Path(dest_prefix)

    snps = read_esi(f"{source_prefix}.esi")
    probes = read_epi(f"{source_prefix}.epi")
    probes_by_id = {p.probe_id: p for p in probes}
    missing = [pid for pid in probe_ids if pid not in probes_by_id]
    if missing:
        raise ValueError(f"probe id(s) not found in source .epi: {missing}")
    selected = [probes_by_id[pid] for pid in probe_ids]

    reader = BESDReader(f"{source_prefix}.besd", len(probes))

    per_probe: list[tuple[ProbeRecord, np.ndarray, np.ndarray, np.ndarray]] = []
    referenced_old_idx: list[int] = []
    seen: set[int] = set()
    for probe in selected:
        snp_idx, betas, ses = reader.get_probe_associations(probe.row_idx)
        per_probe.append((probe, snp_idx, betas, ses))
        for old_idx in snp_idx.tolist():
            if old_idx not in seen:
                seen.add(old_idx)
                referenced_old_idx.append(old_idx)

    # Sorted for a deterministic, position-ordered new .esi (not required by
    # the format, but matches the original file's own convention and keeps
    # output reproducible across runs).
    referenced_old_idx.sort()
    old_to_new = {old: new for new, old in enumerate(referenced_old_idx)}

    dest_prefix.parent.mkdir(parents=True, exist_ok=True)

    with open(f"{dest_prefix}.esi", "w", encoding="utf-8") as fh:
        for old_idx in referenced_old_idx:
            snp = snps[old_idx]
            a1 = snp.a1 if snp.a1 is not None else "NA"
            a2 = snp.a2 if snp.a2 is not None else "NA"
            freq = f"{snp.freq:g}" if snp.freq is not None else "NA"
            fh.write(f"{snp.chromosome}\t{snp.snp_id}\t0\t{snp.bp}\t{a1}\t{a2}\t{freq}\n")

    with open(f"{dest_prefix}.epi", "w", encoding="utf-8") as fh:
        for probe in selected:
            gene = probe.gene if probe.gene is not None else probe.probe_id
            orientation = probe.orientation if probe.orientation is not None else "NA"
            fh.write(
                f"{probe.chromosome}\t{probe.probe_id}\t0\t{probe.probe_bp}\t{gene}\t{orientation}\n"
            )

    n_new_probes = len(selected)
    cols = np.zeros(2 * n_new_probes + 1, dtype=np.int64)
    rowid_parts: list[np.ndarray] = []
    val_parts: list[np.ndarray] = []
    offset = 0
    for i, (_probe, snp_idx, betas, ses) in enumerate(per_probe):
        n = len(snp_idx)
        new_idx = np.fromiter(
            (old_to_new[int(x)] for x in snp_idx.tolist()), dtype=np.uint32, count=n
        )
        cols[2 * i] = offset
        cols[2 * i + 1] = offset + n
        rowid_parts.append(new_idx)
        rowid_parts.append(new_idx)  # SE-position duplicate -- see module docstring.
        val_parts.append(betas.astype(np.float32, copy=False))
        val_parts.append(ses.astype(np.float32, copy=False))
        offset += 2 * n
    cols[2 * n_new_probes] = offset
    val_num = offset

    rowid = np.concatenate(rowid_parts) if rowid_parts else np.empty(0, dtype=np.uint32)
    val = np.concatenate(val_parts) if val_parts else np.empty(0, dtype=np.float32)

    with open(f"{dest_prefix}.besd", "wb") as fh:
        fh.write(struct.pack("<I", _MAGIC_SPARSE_3))
        fh.write(b"\x00" * ((_RESERVED_UNITS - 1) * 4))
        fh.write(struct.pack("<Q", val_num))
        fh.write(cols.tobytes())
        fh.write(rowid.astype(np.uint32, copy=False).tobytes())
        fh.write(val.astype(np.float32, copy=False).tobytes())

    return BesdSubsetResult(
        n_probes=n_new_probes,
        n_snps=len(referenced_old_idx),
        n_associations=val_num // 2,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subset a BESD triple to the probes declared by analyses.tsv."
    )
    parser.add_argument("analyses_tsv", help="Accepted analyses.tsv")
    parser.add_argument("input_prefix", help="Input BESD prefix, without .besd/.epi/.esi")
    parser.add_argument("output_prefix", help="Output BESD prefix, without .besd/.epi/.esi")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = write_besd_subset_from_analyses(
        args.analyses_tsv,
        args.input_prefix,
        args.output_prefix,
    )
    print(
        f"Wrote {result.n_probes} probes, {result.n_snps} SNPs, "
        f"and {result.n_associations} associations to {args.output_prefix}"
    )


if __name__ == "__main__":
    main()
