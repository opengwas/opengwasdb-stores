#!/usr/bin/env python3
"""Construct one matrix-free LD-panel block from diploid genotype dosages.

The public API is :func:`construct_block`.  Genotypes are individuals by
variants with values 0/1/2 (NaN is mean-imputed).  The function deliberately
does not own acquisition: callers may pass an in-memory array or use the CLI,
which asks plink2 to extract one interval from a pfile/bfile.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

# Block-level process parallelism is the only parallel layer. Prevent each
# scipy eigensolver from creating another large BLAS thread pool.
for _thread_var in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS",
):
    os.environ.setdefault(_thread_var, "1")

import numpy as np
import scipy.linalg


@dataclass(frozen=True)
class BlockResult:
    variant_table: list[dict[str, object]]
    eigenvalues: np.ndarray
    eigenvectors: np.ndarray
    provenance: dict[str, object]


def canonical_alid(chrom: object, pos: object, allele1: str, allele2: str) -> str:
    """Return the store ALID, with the two uppercase alleles sorted."""
    chrom = str(chrom).removeprefix("chr")
    a1, a2 = sorted((allele1.upper(), allele2.upper()))
    if not chrom or int(pos) < 1 or not a1 or not a2 or a1 == a2:
        raise ValueError("invalid chromosome, position, or alleles")
    return f"{chrom}:{int(pos)}:{a1}:{a2}"


def variant_alids(variants: list[dict[str, object]]) -> list[str | None]:
    """Each variant's ALID, or None for a record no ALID can name."""
    alids: list[str | None] = []
    for v in variants:
        try: alids.append(canonical_alid(v["chrom"], v["pos"], str(v["allele1"]), str(v["allele2"])))
        except ValueError: alids.append(None)
    return alids


def standardised_ea_dosages(genotypes: np.ndarray, variants: list[dict[str, object]],
                            rows: list[dict[str, object]]) -> tuple[np.ndarray, np.ndarray]:
    """Unit-norm centred effect-allele dosages for panel ``rows``, and their EAF.

    ``genotypes`` and ``variants`` are as :func:`construct_block` takes them;
    each row names its variant by ``SNP`` and its effect allele by ``EA``.
    Missing calls are mean-imputed, as in construction, so for a block built
    from these genotypes ``x.T @ x`` is the LD its eigendecomposition stores.
    """
    index = {alid: i for i, alid in enumerate(variant_alids(variants)) if alid is not None}
    columns = [index[str(r["SNP"])] for r in rows]
    alt = np.asarray(genotypes, dtype=np.float64)[:, columns]
    alt_is_ea = np.array([str(variants[c]["allele2"]).upper() == r["EA"] for c, r in zip(columns, rows)])
    dosage = np.where(alt_is_ea, alt, 2 - alt)
    means = np.nanmean(dosage, axis=0)
    x = np.where(np.isnan(dosage), means, dosage) - means
    return x / np.sqrt((x * x).sum(axis=0)), means / 2


def construct_block(
    genotypes: np.ndarray,
    variants: list[dict[str, object]],
    *,
    individual_ids: list[str] | None = None,
    interval: str = "unknown",
    mac_threshold: int = 50,
    maf_threshold: float | None = None,
    max_missing_rate: float | None = None,
    cumulative_variance_target: float = 0.99,
    component_floor: int = 250,
) -> BlockResult:
    """Filter by one variant floor and return a stable eigendecomposition and provenance.

    The floor is ``maf_threshold`` when given, else ``mac_threshold``; a MAF
    floor replaces the MAC floor rather than adding to it (#241). MAF is the
    minor-allele count over twice the called individuals, so missing calls do
    not dilute it. ``max_missing_rate``, when given, drops variants with a
    larger share of individuals uncalled.
    """
    g = np.asarray(genotypes, dtype=np.float64)
    if g.ndim != 2 or g.shape[1] != len(variants):
        raise ValueError("genotypes must be individuals x variants")
    if individual_ids is not None and len(individual_ids) != g.shape[0]:
        raise ValueError("individual_ids length does not match genotypes")
    if (not 0 < cumulative_variance_target <= 1 or mac_threshold < 0 or component_floor < 0
            or (maf_threshold is not None and not 0 <= maf_threshold <= .5)
            or (max_missing_rate is not None and not 0 <= max_missing_rate <= 1)):
        raise ValueError("invalid construction threshold")
    if g.shape[0] < 2:
        raise ValueError("at least two individuals are required")

    # Mean-impute missing calls before centring.  All-missing variants become
    # non-finite and are excluded along with invariant variants.
    ac = np.nansum(g, axis=0)
    called = np.sum(~np.isnan(g), axis=0)
    means = np.divide(ac, called, out=np.full(g.shape[1], np.nan), where=called > 0)
    filled = np.where(np.isnan(g), means, g)
    mac = np.minimum(ac, 2 * called - ac)
    maf = np.divide(mac, 2 * called, out=np.zeros(g.shape[1]), where=called > 0)
    all_missing = called == 0
    invariant = ~all_missing & (mac == 0)
    polymorphic = ~all_missing & ~invariant
    above = maf >= maf_threshold if maf_threshold is not None else mac >= mac_threshold
    below = polymorphic & ~above
    missing_rate = (g.shape[0] - called) / g.shape[0]
    high_missing = polymorphic & above & (missing_rate > max_missing_rate if max_missing_rate is not None
                                          else np.zeros(g.shape[1], dtype=bool))
    eligible = polymorphic & above & ~high_missing
    # An ALID naming two source records is ambiguous to every consumer: gnomAD
    # splits multi-allelic sites, so the deletion GA>G and the insertion G>GA
    # at one position both become pos:G:GA (#241, #348). Of the eligible
    # records sharing an ALID, keep the one with the higher MAF (then call
    # rate, then input order): the record a GWAS reporting that ALID most
    # likely means.
    alids = variant_alids(variants)
    best: dict[str, int] = {}
    for i in np.flatnonzero(eligible):
        alid = alids[i]
        if alid is None: continue
        j = best.get(alid)
        if j is None or (maf[i], -missing_rate[i]) > (maf[j], -missing_rate[j]): best[alid] = int(i)
    winners = set(best.values())
    collided = np.array([bool(eligible[i]) and a is not None and i not in winners for i, a in enumerate(alids)],
                        dtype=bool)
    kept = np.flatnonzero(eligible & ~collided)
    if not len(kept):
        raise ValueError("no variants remain after variant-floor/invariant filtering")

    x = filled[:, kept]
    x -= x.mean(axis=0)
    scale = np.sqrt(np.sum(x * x, axis=0))
    # Polymorphic yet constant once mean-imputed, e.g. every called individual
    # heterozygous: nothing to correlate, so it is dropped and counted.
    nonzero = scale > 0
    zero_variance = int((~nonzero).sum())
    kept = kept[nonzero]
    x = x[:, nonzero] / scale[nonzero]
    # PLINK --export A emits ALT dosage, while panel rows declare the
    # lexicographically canonical effect allele. Reverse centred dosage when
    # canonical EA is REF so eigenvector rows and reported alleles agree.
    orientation = np.array([
        1.0 if str(variants[int(i)]["allele2"]).upper() ==
        min(str(variants[int(i)]["allele1"]).upper(),
            str(variants[int(i)]["allele2"]).upper()) else -1.0
        for i in kept
    ])
    x *= orientation
    # x.T @ x can be enormous while its rank is bounded by the sample count.
    # A thin SVD gives the same nonzero eigenpairs without allocating the
    # variant-by-variant LD matrix: x.T @ x = V diag(s**2) V.T.
    _, singular_values, vt = scipy.linalg.svd(
        x, full_matrices=False, check_finite=False, lapack_driver="gesdd"
    )
    vals = singular_values * singular_values
    vecs = vt.T
    positive = np.maximum(vals, 0.0)
    total = float(positive.sum()) or 1.0
    target_k = int(np.searchsorted(np.cumsum(positive) / total, cumulative_variance_target)) + 1
    k = min(max(target_k, component_floor), len(vals))
    achieved = float(np.cumsum(positive)[k - 1] / total)

    table: list[dict[str, object]] = []
    for source_i in kept:
        v = variants[int(source_i)]
        a1, a2 = sorted((str(v["allele1"]).upper(), str(v["allele2"]).upper()))
        # EAF is for canonical A1. Dosage is conventionally allele2 dosage.
        alt_freq = float(means[source_i] / 2)
        eaf = alt_freq if a1 == str(v["allele2"]).upper() else 1 - alt_freq
        table.append({
            "CHR": str(v["chrom"]).removeprefix("chr"), "BP": int(v["pos"]),
            "SNP": canonical_alid(v["chrom"], v["pos"], a1, a2),
            "OA": a2, "EA": a1, "EAF": eaf, "MAC": float(mac[source_i]),
        })
    provenance = {
        "interval": interval, "n_individuals": int(g.shape[0]),
        "n_variants_input": int(g.shape[1]), "n_variants_retained": len(table),
        "mac_threshold": None if maf_threshold is not None else mac_threshold,
        "maf_threshold": maf_threshold, "max_missing_rate": max_missing_rate,
        "alid_collision_rule": "keep_highest_maf",
        "n_variants_all_missing": int(all_missing.sum()), "n_variants_invariant": int(invariant.sum()),
        "n_variants_below_floor": int(below.sum()), "n_variants_high_missing": int(high_missing.sum()),
        "n_variants_alid_collision": int(collided.sum()),
        "n_variants_zero_variance": zero_variance,
        "below_floor_mac_counts": {str(int(m)): int(c) for m, c in zip(*np.unique(mac[below], return_counts=True))},
        "missing_call_rate": float(missing_rate.mean()),
        "max_missing_call_rate_retained": float(missing_rate[kept].max()),
        "retained_missing_call_rate_over": {str(c): int((missing_rate[kept] > c).sum()) for c in (.05, .1, .5)},
        "cumulative_variance_target": cumulative_variance_target,
        "component_floor": component_floor, "components_retained": k,
        "achieved_variance": achieved,
    }
    return BlockResult(table, vals, vecs[:, :k], provenance)


def _atomic_outputs(base: Path, result: BlockResult) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    token = next(tempfile._get_candidate_names())
    table_tmp = base.with_name(f".{base.name}.{token}.tsv")
    eig_tmp = base.with_name(f".{base.name}.{token}.ldeig.npz")
    prov_tmp = base.with_name(f".{base.name}.{token}.provenance.json")
    try:
        with table_tmp.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(result.variant_table[0]), delimiter="\t")
            writer.writeheader(); writer.writerows(result.variant_table)
        with eig_tmp.open("wb") as fh:
            np.savez_compressed(fh, values=result.eigenvalues, vectors=result.eigenvectors)
        prov_tmp.write_text(json.dumps(result.provenance, indent=2, sort_keys=True) + "\n")
        table_tmp.replace(base.with_suffix(".tsv"))
        eig_tmp.replace(base.with_suffix(".ldeig.npz"))
        # Provenance is the completion marker and is therefore renamed last.
        prov_tmp.replace(base.with_suffix(".provenance.json"))
    finally:
        for path in (table_tmp, eig_tmp, prov_tmp): path.unlink(missing_ok=True)


def plink_source_args(pfile: Path | None, bfile: Path | None) -> list[str]:
    if pfile is not None:
        result = ["--pfile", str(pfile)]
        if Path(str(pfile) + ".pvar.zst").exists(): result.append("vzs")
        return result
    if bfile is not None: return ["--bfile", str(bfile)]
    raise ValueError("a pfile or bfile is required")


def read_plink_raw(path: Path) -> tuple[np.ndarray, list[str], list[str]]:
    """Read PLINK's additive export without dtype-inferencing metadata fields."""
    with path.open(newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"empty PLINK raw export: {path}") from exc
        if len(header) < 7 or header[1] != "IID":
            raise ValueError(f"unexpected PLINK raw header: {path}")
        names = header[6:]
        ids: list[str] = []
        rows: list[list[float]] = []
        for line_number, fields in enumerate(reader, start=2):
            if len(fields) != len(header):
                raise ValueError(f"malformed PLINK raw row {line_number}: {path}")
            ids.append(fields[1])
            rows.append([np.nan if value in {"NA", ".", ""} else float(value)
                         for value in fields[6:]])
    return np.asarray(rows, dtype=np.float64), names, ids


def _plink_extract(args: argparse.Namespace, work: Path) -> tuple[np.ndarray, list[dict[str, object]], list[str]]:
    prefix = work / "block"
    cmd = [args.plink2, *plink_source_args(args.pfile, args.bfile), "--chr", str(args.chrom),
           "--from-bp", str(args.start), "--to-bp", str(args.end),
           "--keep", str(args.keep), "--make-pgen", "--out", str(prefix)]
    subprocess.run(cmd, check=True)
    subprocess.run([args.plink2, "--pfile", str(prefix), "--export", "A", "--out", str(prefix)], check=True)
    genotypes, names, ids = read_plink_raw(prefix.with_suffix(".raw"))
    pvar = prefix.with_suffix(".pvar")
    # A raw column is named ID_<counted allele>. Matching each ID against every
    # name is quadratic, which a floor-free 200k-variant block cannot afford
    # (#241), so index every prefix that ends at an underscore.
    matchable = set(names)
    for name in names:
        parts = name.split("_")
        matchable.update("_".join(parts[:i]) for i in range(1, len(parts)))
    variants = []
    with pvar.open() as fh:
        for line in fh:
            if line.startswith("##"): continue
            fields = line.rstrip().split("\t")
            if fields[0].lstrip("#") == "CHROM": continue
            chrom, pos, vid, ref, alt = fields[:5]
            if vid in matchable:
                variants.append({"chrom": chrom, "pos": pos, "allele1": ref, "allele2": alt})
    return genotypes, variants, ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pfile", type=Path); src.add_argument("--bfile", type=Path)
    ap.add_argument("--keep", type=Path, required=True); ap.add_argument("--chrom", required=True)
    ap.add_argument("--start", type=int, required=True); ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    floor = ap.add_mutually_exclusive_group()
    floor.add_argument("--mac", type=int, default=50)
    floor.add_argument("--maf", type=float, help="MAF floor over called individuals; replaces --mac")
    ap.add_argument("--max-missing", type=float, help="Drop variants with a larger share of individuals uncalled")
    ap.add_argument("--cumvar", type=float, default=.99); ap.add_argument("--min-k", type=int, default=250)
    ap.add_argument("--plink2", default="plink2")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="ld-block-") as tmp:
        g, variants, ids = _plink_extract(args, Path(tmp))
        result = construct_block(g, variants, individual_ids=ids,
            interval=f"{args.chrom}:{args.start}-{args.end}", mac_threshold=args.mac,
            maf_threshold=args.maf, max_missing_rate=args.max_missing,
            cumulative_variance_target=args.cumvar, component_floor=args.min_k)
    _atomic_outputs(args.out, result)
    return 0


if __name__ == "__main__": raise SystemExit(main())
