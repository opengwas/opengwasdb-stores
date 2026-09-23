#!/usr/bin/env python3
"""Build per-ancestry common-variant axes from gnomAD, streamed, never mirrored.

The Dense Component of a Hybrid Store needs one thing from a reference: the
list of canonical ALIDs its matrix has rows for. It does not need genotypes and
it does not need LD, so mirroring a genotype callset to derive one is the wrong
shape of work -- especially when the callset that is already on disk
(HGDP+1kGP, ~800 samples per ancestry) cannot support a 1% threshold at all:
1% of 772 EUR samples is 15 allele copies.

gnomAD publishes per-ancestry allele frequencies over 76,215 genomes, covering
the sex chromosomes and the mitochondrion that HGDP+1kGP's autosome-only LD
blocks never reached. Its sites VCFs are large (~550 GB) only because of the
VEP annotation this script never reads, and they are bgzip+tabix indexed, so
`bcftools` streams them over HTTPS a region at a time. Nothing is mirrored:
each shard is read remotely and reduced to ALIDs on the way past.

Two frequency facts are kept apart on purpose. `AF_<group>` is the frequency
this script thresholds on; `AN_<group>` is how many alleles were actually
called there, and a group with almost no coverage at a site can report a
confident-looking frequency from a handful of alleles. `--min-an-fraction`
refuses those rather than letting them set the axis.

The mitochondrion is a deliberate exception, recorded rather than smoothed
over: gnomAD's chrM release is a different callset with a different model
(homoplasmy/heteroplasmy, `AF_hom`), and carries no per-ancestry AF fields at
all. Its variants are admitted to every ancestry's axis on the global
homoplasmic frequency, which is a simplification, not a per-ancestry claim.

Output is a plain ALID list per ancestry -- the simplest of the three forms
`opengwasdb.variants.reference.read_variant_reference` accepts -- so the axis
is inspectable with `zcat` and carries no format of its own to drift.

    pixi run python resources/reference-resources/gnomad-v4-common-hg38/build_common_panel.py \
      --out-dir /data/opengwasdb/reference/gnomad-v4-common-hg38 \
      --min-maf 0.01 --jobs 12
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

GNOMAD_V4 = (
    "https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/genomes/"
    "gnomad.genomes.v4.1.sites.{contig}.vcf.bgz"
)
#: gnomAD has no v4 mitochondrial release; v3.1's is the current one.
GNOMAD_MT = (
    "https://storage.googleapis.com/gcp-public-data--gnomad/release/3.1/vcf/genomes/"
    "gnomad.genomes.v3.1.sites.chrM.vcf.bgz"
)

#: gnomAD v4.1 genetic ancestry groups carrying a per-group `AF_`/`AN_` pair.
#: `grpmax` is a derived maximum rather than a group and is deliberately absent.
GNOMAD_GROUPS: tuple[str, ...] = (
    "afr", "ami", "amr", "asj", "eas", "fin", "mid", "nfe", "remaining", "sas",
)

#: Which gnomAD groups compose each registry super-population.
#:
#: EUR takes `nfe` and `fin` only. `asj` and `ami` are European-descended
#: founder populations whose drift would put variants on a European axis that
#: are common in neither of the groups a European release is actually built
#: from; they stay available as their own lists instead. `remaining` is
#: heterogeneous by construction and composes nothing. NAF has no gnomAD group
#: at all -- recorded here as an empty mapping rather than silently omitted, so
#: the gap is visible in the manifest a release reads.
SUPERPOP_COMPOSITION: dict[str, tuple[str, ...]] = {
    "AFR": ("afr",),
    "AMR": ("amr",),
    "EAS": ("eas",),
    "EUR": ("nfe", "fin"),
    "MID": ("mid",),
    "NAF": (),
    "SAS": ("sas",),
}

CONTIGS: tuple[str, ...] = tuple(f"chr{i}" for i in range(1, 23)) + ("chrX", "chrY", "chrM")

#: Shard width for a remote region query. Small enough that one failure is
#: cheap to retry, large enough that per-query overhead stays negligible.
SHARD_MB = 10

#: hg38 primary assembly lengths, for shard planning only.
CONTIG_LENGTHS: dict[str, int] = {
    "chr1": 248956422, "chr2": 242193529, "chr3": 198295559, "chr4": 190214555,
    "chr5": 181538259, "chr6": 170805979, "chr7": 159345973, "chr8": 145138636,
    "chr9": 138394717, "chr10": 133797422, "chr11": 135086622, "chr12": 133275309,
    "chr13": 114364328, "chr14": 107043718, "chr15": 101991189, "chr16": 90338345,
    "chr17": 83257441, "chr18": 80373285, "chr19": 58617616, "chr20": 64444167,
    "chr21": 46709983, "chr22": 50818468, "chrX": 156040895, "chrY": 57227415,
    "chrM": 16569,
}


def canonical_alid(chrom: str, pos: str, ref: str, alt: str) -> str | None:
    """The canonical ALID for one site, or ``None`` if it names no variant.

    Mirrors `opengwasdb.variants.normalise`: strip a `chr` prefix, uppercase the
    non-autosomal labels, and order the allele pair lexically so a source that
    reports the pair the other way round resolves to the same row.
    """
    c = chrom[3:] if chrom.lower().startswith("chr") else chrom
    c = c.upper() if c.upper() in {"X", "Y", "MT", "M"} else c
    ref, alt = ref.strip().upper(), alt.strip().upper()
    if not ref or not alt or ref == alt or ref == "." or alt == ".":
        return None
    if set(ref) - set("ACGT") or set(alt) - set("ACGT"):
        return None
    a1, a2 = (ref, alt) if ref < alt else (alt, ref)
    return f"{c}:{int(pos)}:{a1}:{a2}"


def _query(url: str, region: str, fields: str, retries: int = 4) -> str:
    """One remote bcftools region query, retried on transient transport failure."""
    cmd = ["bcftools", "query", "-r", region, "-f", fields, url]
    last = ""
    for attempt in range(retries):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        last = (proc.stderr or "").strip()
        time.sleep(2 ** attempt)
    raise RuntimeError(f"bcftools query failed for {region}: {last}")


def _maf(value: str) -> float:
    try:
        af = float(value)
    except (TypeError, ValueError):
        return 0.0
    if af != af or not 0.0 <= af <= 1.0:
        return 0.0
    return min(af, 1.0 - af)


def shard_autosomal(job: tuple[str, int, int, float, float]) -> dict[str, set[str]]:
    """Reduce one region of a v4.1 contig to per-group ALID sets."""
    contig, start, end, min_maf, min_an_fraction = job
    fields = "%CHROM\t%POS\t%REF\t%ALT" + "".join(
        f"\t%INFO/AF_{g}\t%INFO/AN_{g}" for g in GNOMAD_GROUPS
    ) + "\n"
    text = _query(GNOMAD_V4.format(contig=contig), f"{contig}:{start}-{end}", fields)

    # An allele number cap per group, taken from this shard's own maximum: the
    # callable set varies by region (and by ploidy on chrX/chrY), so a global
    # constant would reject correct sites at the edges of coverage.
    rows = [line.split("\t") for line in text.splitlines() if line]
    an_cap = [0.0] * len(GNOMAD_GROUPS)
    for r in rows:
        for i in range(len(GNOMAD_GROUPS)):
            try:
                an_cap[i] = max(an_cap[i], float(r[5 + 2 * i]))
            except (TypeError, ValueError, IndexError):
                pass

    out: dict[str, set[str]] = {g: set() for g in GNOMAD_GROUPS}
    for r in rows:
        if len(r) < 4 + 2 * len(GNOMAD_GROUPS):
            continue
        alid = canonical_alid(r[0], r[1], r[2], r[3])
        if alid is None:
            continue
        for i, group in enumerate(GNOMAD_GROUPS):
            if _maf(r[4 + 2 * i]) < min_maf:
                continue
            try:
                an = float(r[5 + 2 * i])
            except (TypeError, ValueError):
                continue
            if an_cap[i] > 0 and an < min_an_fraction * an_cap[i]:
                continue
            out[group].add(alid)
    return out


def mitochondrial(min_maf: float) -> set[str]:
    """Homoplasmic chrM variants above the threshold, from gnomAD v3.1.

    One global set: the mitochondrial release carries no per-ancestry AF, so
    every ancestry's axis receives the same variants. Recorded in the manifest
    as a simplification rather than presented as a per-ancestry result.
    """
    text = _query(GNOMAD_MT, "chrM", "%CHROM\t%POS\t%REF\t%ALT\t%INFO/AF_hom\n")
    found: set[str] = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        alid = canonical_alid(parts[0], parts[1], parts[2], parts[3])
        if alid is not None and _maf(parts[4]) >= min_maf:
            found.add(alid)
    return found


def _alid_sort_key(alid: str) -> tuple[int, str, int, str]:
    """Order ALIDs by chromosome then position, autosomes before X/Y/M."""
    chrom, pos, a1, a2 = alid.split(":", 3)
    rank = int(chrom) if chrom.isdigit() else {"X": 23, "Y": 24, "M": 25, "MT": 25}.get(chrom, 99)
    return rank, chrom, int(pos), f"{a1}:{a2}"


def write_alids(path: Path, alids: set[str]) -> str:
    """Write one sorted ALID list atomically; return its sha256."""
    import hashlib

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    digest = hashlib.sha256()
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        for alid in sorted(alids, key=_alid_sort_key):
            line = alid + "\n"
            fh.write(line)
            digest.update(line.encode("utf-8"))
    tmp.replace(path)
    return digest.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--min-maf", type=float, default=0.01,
                   help="minor allele frequency floor per ancestry group (default 0.01)")
    p.add_argument("--min-an-fraction", type=float, default=0.5,
                   help="reject a site whose group allele number is below this fraction "
                        "of the shard's maximum for that group (default 0.5)")
    p.add_argument("--contigs", nargs="+", default=list(CONTIGS))
    p.add_argument("--jobs", type=int, default=12, help="concurrent remote shard queries")
    p.add_argument("--shard-mb", type=int, default=SHARD_MB)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 0.0 < args.min_maf < 0.5:
        raise SystemExit("--min-maf must be in (0, 0.5)")

    groups: dict[str, set[str]] = {g: set() for g in GNOMAD_GROUPS}
    mt: set[str] = set()

    jobs: list[tuple[str, int, int, float, float]] = []
    for contig in args.contigs:
        if contig == "chrM":
            continue
        length = CONTIG_LENGTHS[contig]
        width = args.shard_mb * 1_000_000
        for start in range(1, length + 1, width):
            jobs.append((contig, start, min(start + width - 1, length), args.min_maf, args.min_an_fraction))

    print(f"gnomAD v4.1 common-variant axis | MAF >= {args.min_maf} | "
          f"AN >= {args.min_an_fraction:.0%} of shard max", flush=True)
    print(f"contigs={len(args.contigs)} shards={len(jobs)} jobs={args.jobs}", flush=True)

    started = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(shard_autosomal, j): j for j in jobs}
        for future in as_completed(futures):
            contig, start, end, *_ = futures[future]
            try:
                for group, alids in future.result().items():
                    groups[group].update(alids)
            except Exception as exc:  # noqa: BLE001 - one shard must not lose the run
                print(f"  FAILED {contig}:{start}-{end}: {exc}", file=sys.stderr, flush=True)
            done += 1
            if done % 25 == 0 or done == len(jobs):
                total = sum(len(v) for v in groups.values())
                print(f"  {done}/{len(jobs)} shards ({time.time()-started:.0f}s) "
                      f"group-variant pairs={total:,}", flush=True)

    if "chrM" in args.contigs:
        mt = mitochondrial(args.min_maf)
        print(f"chrM: {len(mt):,} homoplasmic variants >= {args.min_maf}", flush=True)
        for group in groups:
            groups[group].update(mt)

    out = args.out_dir
    checksums: dict[str, dict[str, object]] = {}

    for group, alids in sorted(groups.items()):
        path = out / "gnomad-groups" / f"{group}-variants.txt.gz"
        checksums[f"gnomad:{group}"] = {
            "path": str(path), "variants": len(alids), "sha256": write_alids(path, alids)
        }
        print(f"  gnomad:{group:<10} {len(alids):>12,}  {path}", flush=True)

    for superpop, members in sorted(SUPERPOP_COMPOSITION.items()):
        if not members:
            print(f"  {superpop:<17} {'-':>12}  no gnomAD group; no axis emitted", flush=True)
            continue
        union: set[str] = set()
        for m in members:
            union |= groups[m]
        path = out / f"{superpop}-variants.txt.gz"
        checksums[superpop] = {
            "path": str(path), "variants": len(union), "composed_of": list(members),
            "sha256": write_alids(path, union),
        }
        print(f"  {superpop:<17} {len(union):>12,}  {path}", flush=True)

    every: set[str] = set()
    for members in SUPERPOP_COMPOSITION.values():
        for m in members:
            every |= groups[m]
    path = out / "ALL-variants.txt.gz"
    checksums["ALL"] = {"path": str(path), "variants": len(every), "sha256": write_alids(path, every)}
    print(f"  {'ALL':<17} {len(every):>12,}  {path}", flush=True)

    manifest = {
        "resource_id": "gnomad-v4-common-hg38",
        "genome_build": "GRCh38",
        "min_maf": args.min_maf,
        "min_an_fraction": args.min_an_fraction,
        "contigs": list(args.contigs),
        "sources": {"autosomal_and_sex": GNOMAD_V4, "mitochondrial": GNOMAD_MT},
        "superpop_composition": {k: list(v) for k, v in SUPERPOP_COMPOSITION.items()},
        "mitochondrial_variants": len(mt),
        "mitochondrial_note": (
            "gnomAD's chrM release carries no per-ancestry AF; these variants are "
            "admitted to every ancestry axis on the global homoplasmic frequency "
            "(AF_hom) and are not a per-ancestry claim."
        ),
        "outputs": checksums,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nmanifest: {out / 'manifest.json'}  ({time.time()-started:.0f}s total)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
