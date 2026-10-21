#!/usr/bin/env python3
"""Cache the pinned gnomAD v3.1.2 HGDP+1kGP GRCh38 genotype callset."""
from __future__ import annotations

import argparse, csv, gzip, json, re, subprocess
from pathlib import Path

BASE = "https://storage.googleapis.com/gcp-public-data--gnomad/release/3.1.2/vcf/genomes"
META = "gnomad.genomes.v3.1.2.hgdp_1kg_subset_sample_meta.tsv.bgz"


def download(url: str, path: Path) -> None:
    """Resumable curl download; a verified .complete marker is the cache key."""
    marker = path.with_name(path.name + ".complete")
    if path.exists() and marker.exists(): return
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["curl", "--fail", "--location", "--retry", "5", "--continue-at", "-",
                             "--output", str(path), url])
    # curl returns 33 when a prior run already downloaded the complete object
    # and the server rejects a range beginning exactly at EOF. Verification is
    # authoritative in either case.
    if result.returncode not in (0, 33): result.check_returncode()


def verify_vcf(path: Path) -> None:
    result = subprocess.run(["bcftools", "view", "--header-only", str(path)],
                            check=True, capture_output=True, text=True)
    header = result.stdout.lower()
    if "grch38" not in header and "grch_38" not in header:
        raise RuntimeError(f"{path} does not declare GRCh38")


def verify_index(path: Path, *, run=subprocess.run) -> None:
    """Exercise the tabix index without requiring optional record-count metadata."""
    match = re.search(r"\.chr([^.]+)\.vcf\.bgz$", path.name)
    if not match: raise RuntimeError(f"cannot derive chromosome from {path.name}")
    region = f"chr{match.group(1)}:1-1"
    run(["bcftools", "view", "--regions", region, "--no-header", "--output-type", "v", str(path)],
        check=True, stdout=subprocess.DEVNULL)


def labels(meta: Path, output: Path) -> None:
    marker = output.with_name(output.name + ".complete")
    if output.exists() and marker.exists(): return
    tmp = output.with_suffix(".tmp")
    with gzip.open(meta, "rt") as source, tmp.open("w", newline="") as dest:
        reader = csv.DictReader(source, delimiter="\t")
        writer = csv.DictWriter(dest, fieldnames=["sample", "population", "project", "genetic_region",
                                                     "high_quality"], delimiter="\t")
        writer.writeheader()
        for row in reader:
            detail = json.loads(row["hgdp_tgp_meta"]) if row["hgdp_tgp_meta"] else {}
            writer.writerow({"sample": row["s"], "population": detail.get("population", ""),
                             "project": detail.get("project", ""),
                             "genetic_region": detail.get("genetic_region", ""),
                             "high_quality": row["high_quality"]})
    tmp.replace(output); marker.write_text("gnomAD-v3.1.2 metadata\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--chromosomes", nargs="+", default=[str(i) for i in range(1, 23)])
    ap.add_argument("--metadata-only", action="store_true")
    args = ap.parse_args()
    meta = args.cache / META
    download(f"{BASE}/{META}", meta)
    # Reading the complete bgzip stream is the metadata integrity check.
    labels(meta, args.cache / "cohort_populations.tsv")
    meta.with_name(meta.name + ".complete").write_text("gnomAD-v3.1.2 metadata\n")
    if args.metadata_only: return 0
    for chrom in args.chromosomes:
        name = f"gnomad.genomes.v3.1.2.hgdp_tgp.chr{chrom}.vcf.bgz"
        vcf = args.cache / name
        download(f"{BASE}/{name}", vcf); download(f"{BASE}/{name}.tbi", Path(str(vcf) + ".tbi"))
        verify_vcf(vcf); verify_index(vcf)
        vcf.with_name(vcf.name + ".complete").write_text("gnomAD-v3.1.2;GRCh38;indexed\n")
        Path(str(vcf) + ".tbi.complete").write_text("gnomAD-v3.1.2 tabix index\n")
    return 0


if __name__ == "__main__": raise SystemExit(main())
