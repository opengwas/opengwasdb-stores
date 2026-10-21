#!/usr/bin/env python3
"""Verify one written LD block against the genotypes it was built from (#241).

The public API is :func:`verify_block`. It reads the artifacts, not the
constructor's in-memory result, and checks: one row per eigenvector row,
canonical unique ALIDs, finite non-negative descending eigenvalues, provenance
agreement, that the stored components reach the consumer's truncation, EAF,
and that every eigenvector row is bound to its TSV row's variant and effect
allele. That binding check is exact: with X the standardised effect-allele
dosages, ``||X v_j||^2`` equals eigenvalue j, which a permuted or flipped row
breaks. The block is then loaded through core's public LD-panel reader.
"""
from __future__ import annotations

import argparse, csv, json, tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from construct_block import _plink_extract, canonical_alid


def components_at(values: np.ndarray, thresh: float) -> int:
    """Components the consumer keeps for ``thresh`` cumulative variance."""
    positive = np.maximum(values, 0.0)
    cumulative = np.cumsum(positive) / (float(positive.sum()) or 1.0)
    return int(np.searchsorted(cumulative, thresh)) + 1


def verify_block(base: Path, genotypes: np.ndarray, variants: list[dict[str, object]], *,
                 thresh: float = 0.9, rtol: float = 1e-6) -> dict[str, object]:
    """Return a report whose ``failures`` lists every failed check by name."""
    from opengwasdb.completion.ld_panel import canonical_panel_alid, list_all_blocks, load_ld_eigenvectors

    with base.with_suffix(".tsv").open(newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    data = np.load(base.with_suffix(".ldeig.npz"))
    values, vectors = data["values"].astype(np.float64), data["vectors"].astype(np.float64)
    provenance = json.loads(base.with_suffix(".provenance.json").read_text())
    failures: list[str] = []
    def require(ok: bool, name: str) -> None:
        if not ok and name not in failures: failures.append(name)

    alids = [r["SNP"] for r in rows]
    require(len(rows) == vectors.shape[0] == provenance["n_variants_retained"], "row_count")
    require(len(set(alids)) == len(alids) and all(
        r["EA"] < r["OA"] and canonical_alid(r["CHR"], r["BP"], r["EA"], r["OA"]) == r["SNP"]
        == canonical_panel_alid(r["SNP"]) for r in rows), "alids")
    finite = bool(np.isfinite(values).all() and np.isfinite(vectors).all())
    require(finite, "finite")
    require(bool((values >= 0).all()), "non_negative")
    require(bool((np.diff(values) <= 0).all()), "descending")
    require(provenance["components_retained"] == vectors.shape[1], "provenance")
    needed = components_at(values, thresh)
    require(needed <= vectors.shape[1], "resolved_at_thresh")

    binding_error = float("nan")
    index = {canonical_alid(v["chrom"], v["pos"], str(v["allele1"]), str(v["allele2"])): i
             for i, v in enumerate(variants)}
    require(all(a in index for a in alids), "binding")
    if finite and "binding" not in failures and "row_count" not in failures:
        columns = [index[a] for a in alids]
        alt = np.asarray(genotypes, dtype=np.float64)[:, columns]
        alt_is_ea = np.array([str(variants[c]["allele2"]).upper() == r["EA"] for c, r in zip(columns, rows)])
        dosage = np.where(alt_is_ea, alt, 2 - alt)
        means = np.nanmean(dosage, axis=0)
        require(np.allclose(means / 2, [float(r["EAF"]) for r in rows], rtol=0, atol=1e-9), "eaf")
        x = np.where(np.isnan(dosage), means, dosage) - means
        x /= np.sqrt((x * x).sum(axis=0))
        projected = ((x @ vectors) ** 2).sum(axis=0)
        binding_error = float(np.max(np.abs(projected - values[:vectors.shape[1]])) / values[0])
        require(binding_error <= rtol, "binding")

    ld_dir, ancestry, chrom = base.parent.parent.parent, base.parent.parent.name, base.parent.name
    blocks = [b for b in list_all_blocks(ld_dir, ancestry, chrom) if b.tsv_path.name == f"{base.name}.tsv"]
    core_k = 0
    require(len(blocks) == 1 and blocks[0].n_ld_snps == len(rows), "core_load")
    if len(blocks) == 1:
        core_values, core_vectors = load_ld_eigenvectors(blocks[0], thresh=thresh)
        core_k = int(core_vectors.shape[1]); k = min(needed, vectors.shape[1])
        require(core_k == k and np.array_equal(core_vectors, vectors[:, :k]) and
                np.array_equal(core_values, values[:k]), "core_load")
    k = vectors.shape[1]
    positive = np.maximum(values, 0.0)
    return {
        "block": f"{chrom}/{base.name}", "failures": failures, "n_variants": len(rows),
        "rank": int((values > values[0] * 1e-10).sum()) if finite else None,
        "components_retained": k, "components_at_thresh": needed, "core_components_at_thresh": core_k,
        "thresh": thresh, "achieved_variance": float(positive[:k].sum() / (positive.sum() or 1.0)),
        "condition_number": float(values[0] / values[k - 1]) if finite and values[k - 1] > 0 else None,
        "binding_relative_error": binding_error,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pfile", type=Path, required=True); ap.add_argument("--keep", type=Path, required=True)
    ap.add_argument("--chrom", required=True)
    ap.add_argument("--start", type=int, required=True); ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--base", type=Path, required=True, help="Block path without suffix")
    ap.add_argument("--thresh", type=float, default=.9); ap.add_argument("--plink2", default="plink2")
    ap.add_argument("--out", type=Path, required=True, help="Report JSON")
    args = ap.parse_args()
    source = SimpleNamespace(plink2=args.plink2, pfile=args.pfile, bfile=None, chrom=args.chrom,
                             start=args.start, end=args.end, keep=args.keep)
    with tempfile.TemporaryDirectory(prefix="ld-verify-") as tmp:
        genotypes, variants, _ = _plink_extract(source, Path(tmp))
    report = verify_block(args.base, genotypes, variants, thresh=args.thresh)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"block": report["block"], "failures": report["failures"]}))
    return 1 if report["failures"] else 0


if __name__ == "__main__": raise SystemExit(main())
