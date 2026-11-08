#!/usr/bin/env python3
"""Verify every row of the rescued `swap_alleles` copies against their raw files (#455).

For each of the swap_alleles studies in `stores/OGS-00011/sidecars/allele-check/summary.tsv`
this reads the raw GWAS-SSF file and the rescued copy side by side and checks, on
every row, that

* the header is identical and the row counts are equal;
* `effect_allele`, `other_allele` and `hm_code` are byte-identical;
* each statistic is the raw value transformed: `beta`/`z`/`Stat` negated,
  `odds_ratio` inverted, `ci_lower`/`ci_upper` inverted and exchanged,
  `effect_allele_frequency` complemented, `MinFreq`/`MaxFreq` complemented and
  exchanged, `Direction` with `+`/`-` exchanged -- and a missing value is
  byte-identical to the raw one;
* every other column is byte-identical.

It does not reuse the rescue's own transformation: the expected values are
recomputed here (exact decimal arithmetic for frequencies and negation).
Writes a TSV receipt with per-file counts and the rescued copy's sha256, and
exits non-zero on any mismatch.

  pixi run -e dev python3 tools/verify_rescued_gwas_files.py \
      --derived-base DIR --receipt receipt.tsv
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import sys
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SUMMARY_TSV = REPO_ROOT / "stores/OGS-00011/sidecars/allele-check/summary.tsv"
SOURCE_READINESS_TSV = REPO_ROOT / "stores/OGS-00011/sidecars/source_readiness.tsv"
RAW_BASE = Path("/data/opengwasdb/raw/ebi-gwas-catalog")
MISSING = {"", "NA"}
NEGATED = ("beta", "z", "Stat")
MAX_REPORTED = 5


def _miss(v: str) -> bool:
    return v.strip() in MISSING


def expect_negated(raw: str) -> str | None:
    """None means 'any string equal as a number to -raw'; missing is returned as is."""
    return raw if _miss(raw) else None


def check_row(hdr: list[str], raw: list[str], new: list[str]) -> list[str]:
    bad: list[str] = []
    ix = {n: i for i, n in enumerate(hdr)}
    handled: set[str] = set()

    def same(name: str, want: str) -> None:
        if new[ix[name]] != want:
            bad.append(f"{name}: {new[ix[name]]!r} != {want!r} (raw {raw[ix[name]]!r})")

    def num_equal(name: str, src: str, fn) -> None:
        got = new[ix[name]]
        if _miss(src):
            if got != src:
                bad.append(f"{name}: missing {src!r} became {got!r}")
            return
        try:
            ok = fn(Decimal(got), Decimal(src))
        except Exception:
            ok = False
        if not ok:
            bad.append(f"{name}: {got!r} is not the transform of raw {src!r}")

    for name in ("effect_allele", "other_allele", "hm_code"):
        same(name, raw[ix[name]])
        handled.add(name)
    for name in NEGATED:
        if name in ix:
            num_equal(name, raw[ix[name]], lambda g, s: g == -s)
            handled.add(name)
    if "effect_allele_frequency" in ix:
        num_equal("effect_allele_frequency", raw[ix["effect_allele_frequency"]], lambda g, s: g == Decimal(1) - s)
        handled.add("effect_allele_frequency")
    if "odds_ratio" in ix:
        num_equal("odds_ratio", raw[ix["odds_ratio"]], lambda g, s: float(g) == 1.0 / float(s))
        handled.add("odds_ratio")
    if "ci_lower" in ix:
        num_equal("ci_lower", raw[ix["ci_upper"]], lambda g, s: float(g) == 1.0 / float(s))
        num_equal("ci_upper", raw[ix["ci_lower"]], lambda g, s: float(g) == 1.0 / float(s))
        handled |= {"ci_lower", "ci_upper"}
    if "MinFreq" in ix:
        num_equal("MinFreq", raw[ix["MaxFreq"]], lambda g, s: g == Decimal(1) - s)
        num_equal("MaxFreq", raw[ix["MinFreq"]], lambda g, s: g == Decimal(1) - s)
        handled |= {"MinFreq", "MaxFreq"}
    if "Direction" in ix:
        same("Direction", raw[ix["Direction"]].translate(str.maketrans("+-", "-+")))
        handled.add("Direction")
    for name in hdr:
        if name not in handled:
            same(name, raw[ix[name]])
    return bad


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file(args: tuple[str, str, str]) -> dict[str, str]:
    aid, raw_path, new_path = args
    rows = mismatching_rows = 0
    examples: list[str] = []
    header_ok = True
    count_ok = True
    with gzip.open(raw_path, "rt", encoding="utf-8", errors="replace") as fr, \
         gzip.open(new_path, "rt", encoding="utf-8") as fn:
        hdr = fr.readline().rstrip("\r\n").split("\t")
        nhdr = fn.readline().rstrip("\r\n").split("\t")
        if hdr != nhdr:
            header_ok = False
        else:
            while True:
                lr, ln = fr.readline(), fn.readline()
                if not lr and not ln:
                    break
                if not lr or not ln:
                    count_ok = False
                    break
                rows += 1
                raw = lr.rstrip("\r\n").split("\t")
                new = ln.rstrip("\r\n").split("\t")
                if len(raw) != len(hdr) or len(new) != len(hdr):
                    bad = [f"field count raw {len(raw)} new {len(new)} header {len(hdr)}"]
                else:
                    bad = check_row(hdr, raw, new)
                if bad:
                    mismatching_rows += 1
                    if len(examples) < MAX_REPORTED:
                        examples.append(f"row {rows}: " + "; ".join(bad))
    return {
        "analysis_id": aid, "rows": str(rows), "mismatching_rows": str(mismatching_rows),
        "header_identical": str(header_ok).lower(), "row_counts_equal": str(count_ok).lower(),
        "sha256": sha256_file(Path(new_path)), "examples": " | ".join(examples),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--derived-base", type=Path, required=True)
    ap.add_argument("--receipt", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args(argv)

    with open(SUMMARY_TSV, newline="") as f:
        ids = [r["analysis_id"] for r in csv.DictReader(f, delimiter="\t")
               if r["gate_reason"] == "eaf_orientation" and r["recommendation"] == "swap_alleles"]
    with open(SOURCE_READINESS_TSV, newline="") as f:
        src = {r["analysis_id"]: r for r in csv.DictReader(f, delimiter="\t")}
    jobs = []
    for aid in sorted(ids):
        raw = Path(src[aid]["data_file"])
        jobs.append((aid, str(raw), str(a.derived_base / raw.relative_to(RAW_BASE))))
    with ProcessPoolExecutor(max_workers=min(a.workers, len(jobs))) as pool:
        results = list(pool.map(verify_file, jobs))
    cols = ["analysis_id", "rows", "mismatching_rows", "header_identical", "row_counts_equal", "sha256", "examples"]
    a.receipt.parent.mkdir(parents=True, exist_ok=True)
    with open(a.receipt, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t")
        w.writeheader()
        w.writerows(results)
    bad = [r for r in results if r["mismatching_rows"] != "0" or r["header_identical"] != "true" or r["row_counts_equal"] != "true"]
    total = sum(int(r["rows"]) for r in results)
    print(f"{len(results)} files, {total} rows, {len(bad)} files with mismatches")
    for r in bad:
        print(f"MISMATCH {r['analysis_id']}: {r['mismatching_rows']} rows; {r['examples']}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
