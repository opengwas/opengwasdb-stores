#!/usr/bin/env python3
"""Build the genome-wide AMR LD Reference Panel (#241), resumably, with receipts.

The owner approved this build on 2026-10-22 after the calibration pilot
(docs/amr-ld-pilot-evidence.md). It builds every block of the EUR layout
(``ld_regions_hg38.amr.tsv``) from the 345 1kGP Phase 3 AMR members of the
cached gnomAD v3.1.2 HGDP+1kGP callset, under the approved variant QC:
- PASS records only;
- at least 80% of members called;
- MAF >= 0.01 over called individuals;
- of the records sharing an ALID, the more frequent.

Stages, each a subprocess whose cost is recorded:
1. membership and preflight, as the pilot does;
2. conversion: each chromosome read once, PASS-only, with sites monomorphic in
   the members dropped (``--mac 1``), at most --readers chromosomes at a time;
3. construction: run_panels with the floor and the call-rate ceiling;
4. verification: verify_block for every built block, then
   find_underresolved_blocks at 0.9;
5. calibration: heldout_calibration on the pilot's five blocks.

Every stage resumes. A converted chromosome carries a .complete marker,
run_panels skips valid blocks built under the same floor, and a block already
verified clean since it was built is not verified again. Each invocation writes
runs/<UTC>/receipt.json, and summary.json covers the panel as it stands.

Run it under ``taskset`` with at most --cpus CPUs, and PYTHONNOUSERSITE=1.
"""
from __future__ import annotations

import argparse, csv, json, os, shutil, sys
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TypeVar
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from acquire_hgdp1kgp import META
from materialize_reference import VCF, conversion_command
from run_amr_pilot import (AMR, HERE, PILOT, Steps, calibration_summary, directory_bytes, environment,
                           preflight_sources, read_ids, reconcile_membership, revision, sha256, utc)

T = TypeVar("T")


def block_label(row: dict[str, str]) -> str: return f"{row['chr']}_{row['start']}-{row['end']}"


# What a converted chromosome's .complete marker said before #354: PASS-only and
# MAC, nothing about which members. Markers are read, never rewritten, so it is
# still what the built panel carries.
LEGACY_MARKER = "plink2 PASS-only biallelic conversion of the AMR members; MAC >= 1"


def run_all(fn: Callable[[T], object], items: Iterable[T], workers: int) -> None:
    """Run ``fn`` over ``items`` on at most ``workers`` threads, all or nothing.

    Submissions stay at most ``workers`` deep, so the first exception -- a step's
    SystemExit, or Ctrl-C's KeyboardInterrupt -- cancels everything still queued
    and no further item starts. Items whose subprocess is already running finish
    before the exception leaves here: killing plink2 mid-chromosome would leave
    a half-written PGEN for the next run to trip over (#354).
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: set[Future[object]] = set()
        try:
            for item in items:
                pending.add(pool.submit(fn, item))
                if len(pending) < workers: continue
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done: future.result()
            for future in pending: future.result()
        except BaseException:
            pool.shutdown(cancel_futures=True)
            raise


def freeze_membership(candidate_dir: Path, members_dir: Path) -> str:
    """Freeze the membership this panel was started with; return its SHA-256.

    make_membership writes a fresh copy into the run directory, so a rerun
    compares a candidate against members/ instead of against itself (#354). A
    first run has nothing to compare and adopts the candidate. A later run must
    match the frozen hash; a mismatch raises before anything is copied, because
    every converted chromosome and built block of this panel was made from the
    members/ bytes the panel was started with.
    """
    frozen = members_dir / "AMR.keep.sha256"
    candidate = sha256(candidate_dir / "AMR.keep")
    if frozen.exists() and frozen.read_text().strip() != candidate:
        raise SystemExit(f"AMR membership differs from the one this panel was started with "
                         f"({candidate} != {frozen.read_text().strip()})")
    members_dir.mkdir(parents=True, exist_ok=True)
    for name in ("AMR.keep", "membership.json"):
        shutil.copy(candidate_dir / name, members_dir / name)
    frozen.write_text(candidate + "\n")
    return candidate


def conversion_marker(pass_only: bool, mac: int | None, members_sha: str | None) -> str:
    """One JSON line naming exactly what a chromosome's conversion was given."""
    return json.dumps({"pass_only": pass_only, "mac": mac, "members_sha256": members_sha}, sort_keys=True) + "\n"


def marker_matches(text: str, expected: str) -> bool:
    """Whether a .complete marker's text records the conversion ``expected`` describes.

    The legacy marker names PASS-only and MAC and nothing else, so it matches a
    PASS-only MAC >= 1 conversion whatever members hash that one carries:
    membership is frozen separately, against the panel's own members/AMR.keep,
    so the omission is not a mismatch and must not force a 59 GB reconversion.
    Any other text must match exactly, which is what makes the marker the
    receipt a rerun can refuse on (#354).
    """
    text, expected = text.strip(), expected.strip()
    if text == expected: return True
    try: described = json.loads(expected)
    except json.JSONDecodeError: return False
    return (text == LEGACY_MARKER and described.get("pass_only") is True and described.get("mac") == 1)


def panel_summary(out: Path, regions_file: Path, calibration: list[dict[str, str]], run: Path, *,
                  maf: float, max_missing: float, n_individuals: int) -> dict[str, object]:
    """The panel as it stands, over every invocation so far.

    Declared blocks come from the full regions file, not from the --chromosomes
    subset this invocation builds: a genome-wide panel's summary names every
    block the panel declares, and a block this host never touched is reported
    from what is on disk rather than left out. Status is read off the disk --
    provenance means built, a recorded "no variants remain" means empty, and
    anything else is a failure -- because only the files survive across runs.
    A verification report counts only when it is younger than the provenance of
    the block it verified: a rebuilt block is unverified until re-verified, and
    saying so beats crediting its predecessor's report (#354).
    """
    with regions_file.open(newline="") as fh: regions = list(csv.DictReader(fh, delimiter="\t"))
    failed: dict[str, str] = {}
    failures = out / "panel/failures.tsv"
    for line in (failures.read_text().splitlines() if failures.exists() else []):
        label, _, error = line.split("\t", 2)
        failed[label] = error  # a later attempt's error replaces an earlier one
    blocks: dict[str, object] = {}
    for row in regions:
        label, report = block_label(row), out / "verify" / f"{block_label(row)}.json"
        base = out / "panel/AMR" / row["chr"] / f"{row['start']}-{row['end']}"
        provenance = base.with_suffix(".provenance.json")
        if provenance.exists():
            p = json.loads(provenance.read_text())
            current = report.exists() and report.stat().st_mtime > provenance.stat().st_mtime
            v = json.loads(report.read_text()) if current else {"failures": ["not_verified"]}
            blocks[label] = {"status": "built", "verification_failures": v["failures"], "provenance": p,
                             **{k: v.get(k) for k in ("rank", "components_at_thresh", "achieved_variance",
                                                      "condition_number", "binding_relative_error")}}
        else:
            error = failed.get(f"AMR/{row['chr']}/{row['start']}-{row['end']}", "not built")
            blocks[label] = {"status": "empty" if "no variants remain" in error else "failed", "error": error}
    built_blocks = [b for b in blocks.values() if b["status"] == "built"]
    classes = ("n_variants_input", "n_variants_all_missing", "n_variants_invariant", "n_variants_below_floor",
               "n_variants_high_missing", "n_variants_alid_collision", "n_variants_zero_variance",
               "n_variants_retained")
    calibrations: dict[str, object] = {}
    for row in calibration:
        label, result = block_label(row), out / "calibration" / f"{block_label(row)}.json"
        # A step that exited non-zero wrote no result: name its log rather than
        # letting the block look uncalibrated.
        calibrations[label] = (calibration_summary(json.loads(result.read_text())) if result.exists()
                               else {"failed": str(run / "logs" / f"calibrate_{label}.log")})
    underresolved = out / "verify/underresolved_0.9.txt"
    return {
        "blocks_declared": len(regions),
        "blocks_by_status": {s: sum(b["status"] == s for b in blocks.values()) for s in ("built", "empty", "failed")},
        "blocks_failing_verification": sorted(k for k, b in blocks.items()
                                              if b["status"] == "built" and b["verification_failures"]),
        "underresolved_at_0.9": [l for l in underresolved.read_text().splitlines() if l] if underresolved.exists() else [],
        "variant_totals": {c: sum(b["provenance"][c] for b in built_blocks) for c in classes},
        "floor_as_approved": all(b["provenance"]["maf_threshold"] == maf and b["provenance"]["mac_threshold"] is None
                                 and b["provenance"]["max_missing_rate"] == max_missing
                                 and b["provenance"].get("alid_collision_rule") == "keep_highest_maf"
                                 and b["provenance"]["n_individuals"] == n_individuals for b in built_blocks),
        "calibration": calibrations,
        "blocks": blocks,
    }


def conversion_counts(log: Path) -> list[str]:
    """plink2's own account of what conversion loaded, filtered and kept."""
    return [line.strip() for line in log.read_text().splitlines()
            if "variant" in line and any(ch.isdigit() for ch in line) and "--" not in line[:2]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference-root", type=Path, default=Path("/data/opengwasdb/reference/hgdp1kgp-hg38"),
                    help="Cache root holding source/ (the AMR members are derived, not read from it)")
    ap.add_argument("--out", type=Path, default=Path("/data/opengwasdb/reference/hgdp1kgp-hg38-amr"),
                    help="Panel root; reused across invocations so the build resumes")
    ap.add_argument("--regions", type=Path, default=AMR / "ld_regions_hg38.amr.tsv")
    ap.add_argument("--mapping", type=Path, default=AMR / "populations_to_superpop.tsv")
    ap.add_argument("--samples", type=Path, default=AMR / "phase3-amr-samples.tsv")
    ap.add_argument("--calibration-blocks", type=Path, default=PILOT / "blocks.tsv")
    ap.add_argument("--chromosomes", nargs="+", default=[str(c) for c in range(1, 23)])
    ap.add_argument("--maf", type=float, default=.01); ap.add_argument("--max-missing", type=float, default=.2)
    ap.add_argument("--readers", type=int, default=2, help="Chromosome VCFs read at once (the epic allows two)")
    ap.add_argument("--plink-threads", type=int, default=4); ap.add_argument("--plink-memory-mb", type=int, default=16000)
    ap.add_argument("--jobs", type=int, default=8, help="Blocks constructed or verified at once")
    ap.add_argument("--replicates", type=int, default=20); ap.add_argument("--cpus", type=int, default=16)
    args = ap.parse_args()
    affinity = len(os.sched_getaffinity(0))
    if affinity > args.cpus:
        raise SystemExit(f"run under taskset with at most {args.cpus} CPUs (affinity is {affinity})")
    if args.readers * args.plink_threads > args.cpus or args.jobs > args.cpus:
        raise SystemExit("--readers x --plink-threads and --jobs must each fit in --cpus")
    if not Path(np.__file__).is_relative_to(sys.prefix):
        raise SystemExit(f"numpy is imported from outside the environment ({np.__file__}); set PYTHONNOUSERSITE=1")
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "1"
    source, out = args.reference_root / "source", args.out
    with args.regions.open(newline="") as fh:
        regions = [r for r in csv.DictReader(fh, delimiter="\t") if r["chr"] in set(args.chromosomes)]
    with args.calibration_blocks.open(newline="") as fh:
        calibration = [r for r in csv.DictReader(fh, delimiter="\t") if r["chr"] in set(args.chromosomes)]
    chromosomes = sorted({r["chr"] for r in regions}, key=int)
    # Everything that can refuse the run does so before anything is written.
    receipt: dict[str, object] = {
        "ticket": "opengwas/opengwasdb-stores#241", "started_utc": utc(), "argv": sys.argv,
        "revision": revision(), "environment": environment(),
        "qc": {"pass_only": True, "maf_threshold": args.maf, "max_missing_rate": args.max_missing,
               "alid_collision_rule": "keep_highest_maf", "conversion_mac": 1},
        # The AMR build reads no EUR.keep: its members are derived from the
        # cohort and the population policy, so an EUR control member list is
        # not an input of this panel (#354).
        "inputs": {str(p): sha256(p) for p in (args.regions, args.mapping, args.samples, args.calibration_blocks,
                                               source / "cohort_populations.tsv", source / META)}}
    run = out / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for directory in ("tmp", "members", "pgen/AMR", "panel", "verify", "calibration"):
        (out / directory).mkdir(parents=True, exist_ok=True)
    # The blocks this run builds, read by run_panels. It lives in the run
    # directory because it is that invocation's subset of the declared panel:
    # a rerun with other --chromosomes must not inherit the previous one.
    run.mkdir(parents=True, exist_ok=True)
    regions_file = run / "regions.tsv"
    with regions_file.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(regions[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader(); writer.writerows(regions)
    env = {**os.environ, "TMPDIR": str(out / "tmp"), "PYTHONNOUSERSITE": "1"}
    steps, py, members = Steps(run, env), sys.executable, out / "members"
    summary: dict[str, object] = {}
    try:
        # 1. Membership, computed fresh in the run directory and then frozen
        # against the panel's own members/, so a rerun cannot compare the panel
        # with itself. Then preflight.
        candidate = run / "members"
        steps.run("membership", [py, str(HERE / "make_membership.py"), "--cohort", str(source / "cohort_populations.tsv"),
                                 "--mapping", str(args.mapping), "--panels", "AMR", "--samples", str(args.samples),
                                 "--out", str(candidate)])
        expected_marker = conversion_marker(pass_only=True, mac=1, members_sha=sha256(candidate / "AMR.keep"))
        # A converted chromosome whose marker records another conversion stops
        # the run here, before members/ or any converted file is touched.
        pending: list[str] = []
        for chrom in chromosomes:
            marker = out / "pgen/AMR" / f"chr{chrom}.complete"
            if not marker.exists(): pending.append(chrom); continue
            if not marker_matches(marker.read_text(), expected_marker):
                raise SystemExit(f"pgen/AMR/chr{chrom}.complete records a different conversion "
                                 f"({marker.read_text().strip()}); refusing to reconvert chr{chrom}")
        keep_sha = freeze_membership(candidate, members)
        amr = read_ids(members / "AMR.keep")
        receipt["membership"] = {**reconcile_membership(amr, args.samples, source / "cohort_populations.tsv",
                                                        source / META), "amr_keep_sha256": keep_sha}
        receipt["sources"] = preflight_sources(source, chromosomes, amr)

        # 2. Conversion, at most --readers chromosome VCFs at once.
        def convert(chrom: str) -> None:
            prefix = out / "pgen/AMR" / f"chr{chrom}"
            steps.run(f"convert_chr{chrom}", conversion_command(
                Path("plink2"), source / VCF.format(chrom), members / "AMR.keep", prefix, 1,
                threads=args.plink_threads, memory_mb=args.plink_memory_mb, pass_only=True))
            for suffix in (".pgen", ".pvar.zst", ".psam"):
                if not Path(f"{prefix}{suffix}").exists(): raise SystemExit(f"conversion wrote no {prefix}{suffix}")
            Path(f"{prefix}.complete").write_text(expected_marker)
        run_all(convert, pending, args.readers)
        receipt["conversion"] = {c: conversion_counts(out / "pgen/AMR" / f"chr{c}.log") for c in chromosomes}

        # 3. Construction.
        steps.run("construct", [py, str(HERE / "run_panels.py"), "--genotype-root", str(out / "pgen"),
                                "--reference-root", str(out / "panel"), "--regions", str(regions_file),
                                "--members", str(members), "--ancestries", "AMR", "--jobs", str(args.jobs),
                                "--maf", str(args.maf), "--max-missing", str(args.max_missing)])
        # 4. Verification of every built block not already verified clean since it was built.
        def base(row: dict[str, str]) -> Path: return out / "panel/AMR" / row["chr"] / f"{row['start']}-{row['end']}"
        def verified(row: dict[str, str]) -> bool:
            report, provenance = out / "verify" / f"{block_label(row)}.json", base(row).with_suffix(".provenance.json")
            return (report.exists() and report.stat().st_mtime > provenance.stat().st_mtime
                    and not json.loads(report.read_text())["failures"])
        built = [r for r in regions if base(r).with_suffix(".provenance.json").exists()]
        def verify(row: dict[str, str]) -> None:
            steps.run(f"verify_{block_label(row)}", [
                py, str(HERE / "verify_block.py"), "--pfile", str(out / "pgen/AMR" / f"chr{row['chr']}"),
                "--keep", str(members / "AMR.keep"), "--chrom", row["chr"], "--start", row["start"],
                "--end", row["end"], "--base", str(base(row)),
                "--out", str(out / "verify" / f"{block_label(row)}.json")], check=False)
        run_all(verify, [r for r in built if not verified(r)], args.jobs)
        steps.run("underresolved_0.9", [py, str(HERE / "find_underresolved_blocks.py"), "--panel", str(out / "panel"),
                                        "--ancestry", "AMR", "--thresh", "0.9",
                                        "--out", str(out / "verify/underresolved_0.9.txt")])

        # 5. Held-out calibration on the pilot's blocks, under the final QC. A
        # result newer than the conversion it was measured from is reused: it
        # costs replicates x two panels per block, and nothing else changed.
        for row in calibration:
            label, result = block_label(row), out / "calibration" / f"{block_label(row)}.json"
            converted = out / "pgen/AMR" / f"chr{row['chr']}.complete"
            if result.exists() and converted.exists() and result.stat().st_mtime > converted.stat().st_mtime:
                continue
            # check=False: a failed calibration is reported as {"failed": log} in
            # the summary, not a reason to abandon the rest of the panel.
            steps.run(f"calibrate_{label}", [
                py, str(HERE / "heldout_calibration.py"), "--pfile", str(out / "pgen/AMR" / f"chr{row['chr']}"),
                "--keep", str(members / "AMR.keep"), "--chrom", row["chr"], "--start", row["start"],
                "--end", row["end"], "--cohort", str(source / "cohort_populations.tsv"), "--maf", str(args.maf),
                "--max-missing", str(args.max_missing), "--replicates", str(args.replicates),
                "--out", str(result)], check=False)

        # The panel as it stands, over every invocation so far.
        summary = panel_summary(out, args.regions, calibration, run, maf=args.maf,
                                max_missing=args.max_missing, n_individuals=len(amr))
        receipt["status"] = "completed"
    except BaseException as exc:
        receipt["status"] = f"stopped: {exc}"
        raise
    finally:
        receipt["ended_utc"] = utc(); receipt["steps"] = steps.records
        receipt["peak_step_rss_mb"] = max((s["max_rss_mb"] for s in steps.records), default=None)
        receipt["output_bytes"] = {p.name: directory_bytes(p) for p in sorted(out.iterdir()) if p.is_dir() and p.name != "runs"}
        (run / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        if summary:
            for path in (run / "summary.json", out / "summary.json"):
                path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"out": str(out), "run": str(run), "status": receipt["status"],
                      "blocks_by_status": summary.get("blocks_by_status")}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
