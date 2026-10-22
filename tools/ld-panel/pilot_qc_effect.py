#!/usr/bin/env python3
"""Reproduce the #241 pilot's variant-QC effect table from the pilot's outputs.

The AMR pilot's evidence doc (`docs/amr-ld-pilot-evidence.md`, "Owner decisions
after the pilot") reports what each owner-approved variant-QC rule keeps of the
five pilot blocks: the MAF floor, FILTER PASS, the 80% call-rate ceiling, and
one record per shared ALID. This recomputes that table from the pilot's own
outputs, so the numbers in the doc are rendered from a tool and a JSON rather
than copied out of a terminal.

Inputs, all under ``--pilot``:

- ``pgen/AMR/chr<c>``, the pilot's pfiles, converted with no frequency floor so
  the floor is applied here rather than at conversion;
- ``vcf/<c>_<start>-<end>.records.tsv``, every biallelic record of the block
  region as ``chrom  pos  ref  alt  FILTER`` (no header), with the same REF/ALT
  the pfiles carry, so a record and a panel row can be joined;
- ``summary.json``, the pilot's own retained count per block, which is under
  the pre-QC drop-all collision rule; it is reported beside the recomputed
  numbers and never used to derive them.

Block definitions come from this repository's ``amr-pilot/blocks.tsv``.

Method: plink2 ``--freq --missing variant-only`` per chromosome into a temporary
directory, then join frequencies, missingness and FILTER by position and
alleles. Counting, in the order the doc's table states it:

1. at least one member called and MAF >= 0.01;
2. ... and FILTER PASS, and at most 20% of members uncalled (80% called);
3. the remaining rows grouped by canonical ALID are the collision groups;
4. a group of two or more loses every row under the pilot's drop-all rule, and
   all but the highest-MAF row under the owner's keep-one rule.

Usage:
  pixi run -e dev python3 tools/ld-panel/pilot_qc_effect.py \\
      --out docs/data/amr-ld-pilot-qc-effect.json

The check is the exact reproduction of the doc's table: 47,466 / 38,891 /
938 / 37,015 / 37,953, and a median MAF gap across collision partners of about
0.10.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from construct_block import canonical_alid

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PILOT = Path("/data/opengwasdb/work/epic232/229/amr-pilot-20261021T234743Z")
BLOCKS = REPO / "resources/reference-resources/hgdp1kgp-hg38-ld/amr-pilot/blocks.tsv"
MAF_FLOOR = 0.01
MAX_MISSING = 0.2


def read_blocks(path: Path) -> list[dict[str, str]]:
    """The pilot's frozen block table: one block per chromosome."""
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def frequencies_and_missingness(plink2: str, pfile: Path, threads: int, out: Path,
                                chrom: str) -> list[dict[str, object]]:
    """plink2's per-variant allele frequency and missingness, joined on variant ID.

    Neither report carries a base-pair position, so it is read from the ID the
    pfiles were converted with (``@:#:$r:$a``, materialize_reference.py):
    ``chrom:pos:REF:ALT``. A variant ID this tool cannot read a position out of
    is a refusal, never a skipped variant.
    """
    argv = [plink2, "--pfile", str(pfile), "vzs", "--freq", "--missing", "variant-only",
            "--threads", str(threads), "--out", str(out)]
    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows: list[dict[str, object]] = []
    # The two reports are the same variant list in the same order, so zipping
    # them blind would pair a variant with its neighbour's missingness if one
    # report ever gained a row. Refuse a disagreement instead.
    with Path(f"{out}.afreq").open(newline="") as fa, Path(f"{out}.vmiss").open(newline="") as fm:
        for freq, miss in zip(csv.DictReader(fa, delimiter="\t"), csv.DictReader(fm, delimiter="\t")):
            if freq["ID"] != miss["ID"]:
                raise SystemExit(f"plink2 reports disagree on variant order at {freq['ID']} / {miss['ID']}")
            fields = freq["ID"].split(":")
            if len(fields) != 4 or not fields[1].isdigit():
                raise SystemExit(f"unreadable variant ID {freq['ID']} in {pfile}")
            rows.append({"chrom": chrom, "pos": int(fields[1]), "ref": freq["REF"],
                         "alt": freq["ALT"], "alt_freqs": freq["ALT_FREQS"],
                         "obs": int(freq["OBS_CT"]), "f_miss": float(miss["F_MISS"])})
    return rows


def record_filters(path: Path) -> dict[tuple[int, str, str], str]:
    """VCF FILTER of every biallelic record in the block region, by (pos, REF, ALT)."""
    filters: dict[tuple[int, str, str], str] = {}
    for _chrom, pos, ref, alt, filt in (line.rstrip("\n").split("\t") for line in path.read_text().splitlines()):
        if "," in alt:
            continue  # plink2 kept biallelic records only, so there is no FILTER to join to
        filters[(int(pos), ref, alt)] = filt
    return filters


def block_rows(plink: list[dict[str, object]], filters: dict[tuple[int, str, str], str],
               start: int, end: int) -> list[dict[str, object]]:
    """Every pfile variant in the block region, with its ALID, FILTER, MAF and call rate."""
    rows = []
    for variant in plink:
        pos = int(variant["pos"])  # type: ignore[arg-type]
        if not start <= pos <= end:
            continue
        key = (pos, str(variant["ref"]), str(variant["alt"]))
        if key not in filters:
            raise SystemExit(f"no VCF record for pfile variant {pos}:{variant['ref']}:{variant['alt']}")
        alt_freq = variant["alt_freqs"]
        # A monomorphic or all-missing site has no ALT_FREQS; MAF 0 keeps it below
        # every floor rather than inventing a frequency for it.
        maf = min(float(alt_freq), 1 - float(alt_freq)) if alt_freq != "NA" else 0.0  # type: ignore[arg-type]
        rows.append({
            "alid": canonical_alid(str(variant["chrom"]), pos, str(variant["ref"]), str(variant["alt"])),
            "pass": filters[key] == "PASS",
            "f_miss": variant["f_miss"],
            "maf": maf,
            "obs": variant["obs"],
        })
    return rows


def qc_effect(rows: list[dict[str, object]]) -> dict[str, object]:
    """Apply the owner's three rules in order and count what survives each."""
    floor = [r for r in rows if r["obs"] > 0 and r["maf"] >= MAF_FLOOR]
    qc = [r for r in floor if r["pass"] and r["f_miss"] <= MAX_MISSING]
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in qc:
        groups[str(row["alid"])].append(row)
    collisions = {alid: members for alid, members in groups.items() if len(members) > 1}
    # How far apart the two records of a pair are, which says whether an ALID
    # that names two variants names two genuinely different ones. A group of
    # more than two has no single gap and is reported by count instead.
    gaps = []
    for members in collisions.values():
        if len(members) == 2:
            gaps.append(abs(float(members[0]["maf"]) - float(members[1]["maf"])))
    return {
        "records_maf": len(floor),
        "records_maf_pass_call_rate": len(qc),
        "collision_groups": len(collisions),
        "collision_records": sum(len(m) for m in collisions.values()),
        "groups_above_two_records": sum(len(m) > 2 for m in collisions.values()),
        "retained_drop_all": sum(len(m) == 1 for m in groups.values()),
        "retained_keep_one": len(groups),
        "maf_gaps": gaps,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pilot", type=Path, default=PILOT, help="The pilot's output directory")
    ap.add_argument("--blocks", type=Path, default=BLOCKS, help="The pilot's frozen block table")
    ap.add_argument("--out", type=Path, required=True, help="Result JSON")
    ap.add_argument("--plink2", default="plink2", help="plink2 executable (default: resolved from PATH)")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()
    blocks = read_blocks(args.blocks)
    summary = json.loads((args.pilot / "summary.json").read_text())
    result: dict[str, object] = {"ticket": "opengwas/opengwasdb-stores#355", "pilot": str(args.pilot),
                                 "blocks": str(args.blocks.relative_to(REPO)) if args.blocks.is_relative_to(REPO)
                                 else str(args.blocks), "maf_floor": MAF_FLOOR,
                                 "max_missing": MAX_MISSING, "blocks_table": {}}
    totals = defaultdict(int)
    gaps: list[float] = []
    versions = subprocess.run([args.plink2, "--version"], capture_output=True, text=True, check=True).stdout
    result["plink2"] = {"version": versions.splitlines()[0], "command": args.plink2}
    with tempfile.TemporaryDirectory(prefix="amr-pilot-qc-") as tmp:
        for block in blocks:
            chrom, start, end = block["chr"], int(block["start"]), int(block["end"])
            table = args.pilot / "vcf" / f"{chrom}_{start}-{end}.records.tsv"
            plink = frequencies_and_missingness(
                args.plink2, args.pilot / "pgen" / "AMR" / f"chr{chrom}", args.threads,
                Path(tmp) / f"chr{chrom}", chrom)
            measured = qc_effect(block_rows(plink, record_filters(table), start, end))
            gaps.extend(measured.pop("maf_gaps"))
            pilot_retained = summary["blocks"][f"{chrom}_{start}-{end}"]["provenance"]["n_variants_retained"]
            result["blocks_table"][f"chr{chrom}:{start}-{end}"] = {
                **measured, "pilot_retained_before_qc": pilot_retained}
            for name, value in measured.items():
                totals[name] += value
    totals["collision_partner_maf_gap_median"] = statistics.median(gaps) if gaps else None
    result["totals"] = dict(totals)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result["totals"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
