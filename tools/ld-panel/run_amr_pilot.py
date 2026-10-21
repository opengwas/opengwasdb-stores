#!/usr/bin/env python3
"""Run the #241 AMR LD calibration pilot end to end, with receipts.

This is calibration evidence, not a panel. It builds the frozen EUR-layout
blocks from the 1kGP AMR samples of the cached gnomAD v3.1.2 HGDP+1kGP
callset into a new output directory and changes no production panel,
population mapping or Store. Each step is a subprocess whose argv, UTC times,
wall time, CPU time, peak RSS and exit status are recorded in receipt.json:

1. membership: the Phase 3 AMR IDs at gnomAD high quality, reconciled with
   gnomAD's relatedness and subcontinental-outlier metadata (reported, not
   filtered on);
2. preflight: GRCh38 header, tabix index, source identity and every member in
   each VCF header (the implementation revision is recorded before any step);
3. extraction: each block's region and members read by index, converted with
   no frequency floor, so the floor is construct_block's alone;
4. construction: run_panels with the MAF floor and one worker;
5. verification: verify_block per block and find_underresolved_blocks at 0.9;
6. calibration: heldout_calibration per block;
7. boundaries: boundary_ld either side of each block edge, for the AMR members
   and for the HGDP+1kGP EUR calibration control's members.

Run it under ``taskset`` with at most --cpus CPUs; it refuses a wider affinity.
"""
from __future__ import annotations

import argparse, csv, gzip, hashlib, importlib.metadata, json, os, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from acquire_hgdp1kgp import META, verify_index, verify_vcf
from materialize_reference import VCF, conversion_command, region_vcf_command

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PILOT = REPO / "resources/reference-resources/hgdp1kgp-hg38-ld/amr-pilot"
MAC_STRATA = ((7, 9), (10, 19), (20, 49), (50, None))


def utc() -> str: return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""): digest.update(chunk)
    return digest.hexdigest()


def read_ids(keep: Path) -> list[str]:
    return [line for line in keep.read_text().splitlines() if line and not line.startswith("#")]


class Steps:
    """Run subprocesses one at a time and record what each cost."""
    def __init__(self, out: Path, env: dict[str, str]):
        self.out, self.env, self.records = out, env, []
        (out / "logs").mkdir(parents=True)

    def run(self, name: str, argv: list[str], *, check: bool = True) -> int:
        log = self.out / "logs" / f"{name}.log"
        started, clock = utc(), time.monotonic()
        with log.open("w") as fh:
            proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, env=self.env)
            # wait4's rusage covers the step's whole process tree, so a step's
            # peak RSS includes the plink2 calls its children make.
            _, status, usage = os.wait4(proc.pid, 0)
        proc.returncode = code = os.waitstatus_to_exitcode(status)
        self.records.append({
            "step": name, "argv": argv, "started_utc": started, "ended_utc": utc(),
            "wall_s": round(time.monotonic() - clock, 2), "user_s": round(usage.ru_utime, 2),
            "sys_s": round(usage.ru_stime, 2), "max_rss_mb": round(usage.ru_maxrss / 1024, 1),
            "exit_code": code, "log": str(log)})
        if code and check: raise SystemExit(f"step {name} exited {code}; see {log}")
        return code


def source_identity(vcf: Path) -> dict[str, object]:
    """Identity short of hashing a 100+ GB file: size, mtime, cache marker, index SHA-256."""
    tbi, stat = Path(f"{vcf}.tbi"), vcf.stat()
    return {"vcf": str(vcf), "size": stat.st_size,
            "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "complete_marker": Path(f"{vcf}.complete").read_text().strip(),
            "tbi_sha256": sha256(tbi), "tbi_size": tbi.stat().st_size}


def reconcile_membership(members: list[str], allowlist: Path, cohort: Path, meta: Path) -> dict[str, object]:
    with allowlist.open(newline="") as fh: phase3 = {r["sample"]: r["pop"] for r in csv.DictReader(fh, delimiter="\t")}
    with cohort.open(newline="") as fh: labels = {r["sample"]: r for r in csv.DictReader(fh, delimiter="\t")}
    chosen, detail = set(members), {}
    with gzip.open(meta, "rt", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if row["s"] in phase3: detail[row["s"]] = row
    pairs = {}
    for sample in sorted(chosen):
        for other in json.loads(detail[sample]["relatedness_inference"])["related_samples"] or []:
            if other["s"] in chosen:
                pairs[tuple(sorted((sample, other["s"])))] = {k: other[k] for k in ("kin", "ibd0", "ibd1", "ibd2")}
    flagged = [s for s in sorted(chosen) if json.loads(detail[s]["relatedness_inference"])["related"]]
    outliers = [s for s in sorted(chosen)
                if (json.loads(detail[s]["hgdp_tgp_meta"])["subcontinental_pca"] or {}).get("outlier")]
    return {
        "phase3_amr_ids": len(phase3), "members": len(chosen),
        "members_by_population": {p: sum(labels[s]["population"] == p for s in chosen) for p in sorted(set(phase3.values()))},
        "excluded": {s: f"high_quality={labels[s]['high_quality']}" for s in sorted(set(phase3) - chosen)},
        "projects": sorted({labels[s]["project"] for s in chosen}),
        "gnomad_related_flag": flagged,
        "related_pairs_within_members": [{"samples": list(k), **v} for k, v in sorted(pairs.items())],
        "subcontinental_pca_outliers": outliers,
        "note": "Relatedness and outliers are reported, not filtered on: no kinship threshold is owner-approved (#241).",
    }


def filter_composition(records: Path, table: Path, start: int, end: int) -> dict[str, dict[str, int]]:
    """FILTER values of the biallelic input records in [start, end] and of the retained variants."""
    from construct_block import canonical_alid
    biallelic = [line.split("\t") for line in records.read_text().splitlines()]
    biallelic = [r for r in biallelic if "," not in r[3] and start <= int(r[1]) <= end]
    # Retained ALIDs name one record each (construct_block excludes collisions).
    by_alid = {canonical_alid(chrom, pos, ref, alt): flt for chrom, pos, ref, alt, flt in biallelic}
    with table.open(newline="") as fh: kept = [r["SNP"] for r in csv.DictReader(fh, delimiter="\t")]
    count = lambda values: {k: values.count(k) for k in sorted(set(values))}
    return {"input_biallelic": count([r[4] for r in biallelic]), "retained": count([by_alid[a] for a in kept])}


def retained_distribution(table: Path) -> dict[str, object]:
    with table.open(newline="") as fh: rows = list(csv.DictReader(fh, delimiter="\t"))
    mac = np.array([float(r["MAC"]) for r in rows]); eaf = np.array([float(r["EAF"]) for r in rows])
    maf = np.minimum(eaf, 1 - eaf)
    strata = {f"{lo}-{hi}" if hi else f">={lo}": int(((mac >= lo) & ((mac <= hi) if hi else True)).sum())
              for lo, hi in MAC_STRATA}
    strata["<7"] = int((mac < 7).sum())
    return {"mac_strata": strata, "maf_quantiles": {q: float(np.quantile(maf, float(q)))
                                                    for q in ("0", "0.05", "0.25", "0.5")}}


def calibration_summary(result: dict[str, object]) -> dict[str, object]:
    """Per panel: acceptance, and r both over accepted replicates and with rejected ones scored 0."""
    summary: dict[str, object] = {k: result[k] for k in ("n_train", "n_test", "n_shared_variants")}
    if result.get("skipped"): return {**summary, "skipped": result["skipped"]}
    median = lambda values: float(np.median(values)) if values else None
    for panel in ("held_out", "test_in_sample"):
        rows = [r for r in result["replicates"] if r["panel"] == panel]
        accepted = [r for r in rows if r["accepted"]]
        summary[panel] = {"replicates": len(rows), "accepted": len(accepted),
                          "median_pearson_r_rejected_as_0": median([r["pearson_r"] if r["accepted"] else 0.0
                                                                    for r in rows]),
                          "median_pearson_r_accepted": median([r["pearson_r"] for r in accepted]),
                          "min_pearson_r_accepted": min((r["pearson_r"] for r in accepted), default=None),
                          "median_rmse_accepted": median([r["rmse"] for r in accepted]),
                          "median_slope_accepted": median([r["slope"] for r in accepted]),
                          **result["panels"][panel]}
    return summary


def directory_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def environment() -> dict[str, object]:
    # Every step, the shared acquisition checks included, runs plink2 and
    # bcftools from PATH, so PATH's are the binaries recorded.
    def version(tool: str) -> str:
        return subprocess.run([tool, "--version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    return {
        "python": sys.executable, "python_version": sys.version.split()[0],
        "packages": {p: importlib.metadata.version(p) for p in ("numpy", "scipy", "scikit-learn", "opengwasdb")},
        "numpy_path": np.__file__,
        "tools": {t: {"path": shutil.which(t), "version": version(t)} for t in ("plink2", "bcftools")},
        "cpu_affinity": len(os.sched_getaffinity(0)),
        "blas_threads": {v: os.environ.get(v) for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")},
    }


def revision() -> dict[str, str]:
    git = lambda *a: subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True, check=True).stdout.strip()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("tracked files are modified; the pilot must run from a committed revision")
    return {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference-root", type=Path, default=Path("/data/opengwasdb/reference/hgdp1kgp-hg38"))
    ap.add_argument("--out", type=Path, required=True, help="New directory for every output and receipt")
    ap.add_argument("--blocks", type=Path, default=PILOT / "blocks.tsv")
    ap.add_argument("--mapping", type=Path, default=PILOT / "populations_to_superpop.tsv")
    ap.add_argument("--samples", type=Path, default=PILOT / "phase3-amr-samples.tsv")
    ap.add_argument("--maf", type=float, default=.01); ap.add_argument("--window", type=int, default=250_000)
    ap.add_argument("--replicates", type=int, default=20); ap.add_argument("--cpus", type=int, default=4)
    args = ap.parse_args()
    if len(os.sched_getaffinity(0)) > args.cpus:
        raise SystemExit(f"run under taskset with at most {args.cpus} CPUs (affinity is {len(os.sched_getaffinity(0))})")
    if not Path(np.__file__).is_relative_to(sys.prefix):
        raise SystemExit(f"numpy is imported from outside the environment ({np.__file__}); set PYTHONNOUSERSITE=1")
    with args.blocks.open(newline="") as fh: blocks = list(csv.DictReader(fh, delimiter="\t"))
    # Genotypes are converted to one chrN prefix per chromosome, the layout run_panels reads.
    chromosomes = [b["chr"] for b in blocks]
    if len(set(chromosomes)) != len(chromosomes): raise SystemExit("--blocks may hold one block per chromosome")
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "1"
    source, out = args.reference_root / "source", args.out
    # Everything that can refuse the run does so before the output directory exists.
    receipt: dict[str, object] = {
        "ticket": "opengwas/opengwasdb-stores#241", "started_utc": utc(), "argv": sys.argv,
        "revision": revision(), "environment": environment(),
        "non_production": "Calibration evidence only; no production panel, mapping or Store changes.",
        "inputs": {str(p): sha256(p) for p in (args.blocks, args.mapping, args.samples,
                                               source / "cohort_populations.tsv", source / META,
                                               args.reference_root / "members/EUR.keep")}}
    out.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "TMPDIR": str(out / "tmp"), "PYTHONNOUSERSITE": "1"}
    for directory in ("tmp", "vcf", "verify", "calibration", "boundary"): (out / directory).mkdir()
    steps, py, members = Steps(out, env), sys.executable, out / "members"
    summary: dict[str, object] = {"blocks": {}, "boundaries": []}
    try:
        # 1-2. Membership, then preflight of every source the blocks need.
        steps.run("membership", [py, str(HERE / "make_membership.py"), "--cohort", str(source / "cohort_populations.tsv"),
                                 "--mapping", str(args.mapping), "--panels", "AMR", "--samples", str(args.samples),
                                 "--out", str(members)])
        amr = read_ids(members / "AMR.keep")
        shutil.copy(args.reference_root / "members/EUR.keep", members / "EUR.keep")
        eur = read_ids(members / "EUR.keep")
        (members / "AMR.ids").write_text("\n".join(amr) + "\n")
        (members / "AMR+EUR.ids").write_text("\n".join(amr + eur) + "\n")
        receipt["membership"] = {**reconcile_membership(amr, args.samples, source / "cohort_populations.tsv", source / META),
                                 "amr_keep_sha256": sha256(members / "AMR.keep"),
                                 "eur_control_members": len(eur), "eur_keep_sha256": sha256(members / "EUR.keep")}
        sources = {}
        for chrom in sorted({b["chr"] for b in blocks}, key=int):
            vcf = source / VCF.format(chrom)
            verify_vcf(vcf); verify_index(vcf)
            header = set(subprocess.run(["bcftools", "query", "-l", str(vcf)], capture_output=True,
                                        text=True, check=True).stdout.split())
            absent = sorted(set(amr + eur) - header)
            if absent: raise SystemExit(f"members absent from {vcf.name}: {absent}")
            sources[chrom] = {**source_identity(vcf), "grch38_header": True, "index_query": True,
                              "all_members_in_header": True}
        receipt["sources"] = sources
        # Informational: why the cached per-ancestry PGENs cannot serve the pilot.
        cached = args.reference_root / "pgen"
        receipt["cached_pgen_not_used"] = {
            p.name: sorted({m.read_text().strip() for m in p.glob("*.complete")})
            for p in (sorted(cached.iterdir()) if cached.is_dir() else []) if p.is_dir()}

        # 3-4. Extract and convert each block, then construct with the MAF floor.
        for b in blocks:
            label, region = f"{b['chr']}_{b['start']}-{b['end']}", f"chr{b['chr']}:{b['start']}-{b['end']}"
            block_vcf = out / "vcf" / f"{label}.vcf.gz"
            steps.run(f"extract_{label}", region_vcf_command(Path("bcftools"), source / VCF.format(b["chr"]),
                                                             region, members / "AMR.ids", block_vcf))
            steps.run(f"records_{label}", ["bcftools", "query", "-f", "%CHROM\t%POS\t%REF\t%ALT\t%FILTER\n",
                                           "-o", str(out / "vcf" / f"{label}.records.tsv"), str(block_vcf)])
            (out / "pgen/AMR").mkdir(parents=True, exist_ok=True)
            steps.run(f"convert_{label}", conversion_command(Path("plink2"), block_vcf, members / "AMR.keep",
                                                             out / "pgen/AMR" / f"chr{b['chr']}", None,
                                                             threads=args.cpus, memory_mb=16000))
        steps.run("construct", [py, str(HERE / "run_panels.py"), "--genotype-root", str(out / "pgen"),
                                "--reference-root", str(out / "panel"), "--regions", str(args.blocks),
                                "--members", str(members), "--ancestries", "AMR", "--jobs", "1",
                                "--maf", str(args.maf)])
        failures = (out / "panel/failures.tsv").read_text()
        if failures: raise SystemExit(f"block construction failed:\n{failures}")

        # 5-6. Verify, then calibrate, each block.
        steps.run("underresolved_0.9", [py, str(HERE / "find_underresolved_blocks.py"), "--panel", str(out / "panel"),
                                        "--ancestry", "AMR", "--thresh", "0.9",
                                        "--out", str(out / "verify/underresolved_0.9.txt")])
        for b in blocks:
            label = f"{b['chr']}_{b['start']}-{b['end']}"
            base = out / "panel/AMR" / b["chr"] / f"{b['start']}-{b['end']}"
            pfile = ["--pfile", str(out / "pgen/AMR" / f"chr{b['chr']}"), "--keep", str(members / "AMR.keep"),
                     "--chrom", b["chr"], "--start", b["start"], "--end", b["end"]]
            verified = steps.run(f"verify_{label}", [py, str(HERE / "verify_block.py"), *pfile, "--base", str(base),
                                                     "--out", str(out / "verify" / f"{label}.json")], check=False)
            steps.run(f"calibrate_{label}", [py, str(HERE / "heldout_calibration.py"), *pfile,
                                             "--cohort", str(source / "cohort_populations.tsv"), "--maf", str(args.maf),
                                             "--replicates", str(args.replicates),
                                             "--out", str(out / "calibration" / f"{label}.json")])
            provenance = json.loads(base.with_suffix(".provenance.json").read_text())
            floor_ok = (provenance["maf_threshold"] == args.maf and provenance["mac_threshold"] is None
                        and provenance["n_individuals"] == len(amr))
            report = out / "verify" / f"{label}.json"
            # A verifier that crashed wrote no report: record that rather than stop the pilot.
            verification = (json.loads(report.read_text()) if report.exists() else
                            {"failures": ["verifier_crashed"], "log": str(out / "logs" / f"verify_{label}.log")})
            summary["blocks"][label] = {
                "boundary_layout": b["boundary_layout"], "provenance": provenance, "floor_as_approved": floor_ok,
                "verification": verification, "verification_exit": verified,
                "filters": filter_composition(out / "vcf" / f"{label}.records.tsv", base.with_suffix(".tsv"),
                                              int(b["start"]), int(b["end"])),
                "retained": retained_distribution(base.with_suffix(".tsv")),
                "calibration": calibration_summary(json.loads((out / "calibration" / f"{label}.json").read_text())),
            }

        # 7. LD across each block edge, in AMR and in the EUR control cohort.
        edges = sorted({(b["chr"], int(b[side])) for b in blocks for side in ("start", "end")},
                       key=lambda e: (int(e[0]), e[1]))
        for chrom, edge in edges:
            label = f"{chrom}_{edge}"
            edge_vcf = out / "vcf" / f"edge_{label}.vcf.gz"
            steps.run(f"extract_edge_{label}", region_vcf_command(
                Path("bcftools"), source / VCF.format(chrom),
                f"chr{chrom}:{max(1, edge - args.window)}-{edge + args.window}", members / "AMR+EUR.ids", edge_vcf))
            for cohort in ("AMR", "EUR"):
                prefix = out / "pgen" / f"edge-{cohort}" / label; prefix.parent.mkdir(parents=True, exist_ok=True)
                steps.run(f"convert_edge_{cohort}_{label}", conversion_command(
                    Path("plink2"), edge_vcf, members / f"{cohort}.keep", prefix, None,
                    threads=args.cpus, memory_mb=16000))
                result = out / "boundary" / f"{cohort}_{label}.json"
                steps.run(f"boundary_{cohort}_{label}", [py, str(HERE / "boundary_ld.py"), "--pfile", str(prefix),
                                                         "--keep", str(members / f"{cohort}.keep"), "--chrom", chrom,
                                                         "--edge", str(edge), "--window", str(args.window),
                                                         "--maf", str(args.maf), "--out", str(result)])
                summary["boundaries"].append({"cohort": cohort, **json.loads(result.read_text())})
        receipt["status"] = "completed"
    except BaseException as exc:
        receipt["status"] = f"stopped: {exc}"
        raise
    finally:
        receipt["ended_utc"] = utc(); receipt["steps"] = steps.records
        receipt["output_bytes"] = {p.name: directory_bytes(p) for p in sorted(out.iterdir()) if p.is_dir()}
        receipt["peak_step_rss_mb"] = max((s["max_rss_mb"] for s in steps.records), default=None)
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"out": str(out), "status": receipt["status"]})); return 0


if __name__ == "__main__": raise SystemExit(main())
