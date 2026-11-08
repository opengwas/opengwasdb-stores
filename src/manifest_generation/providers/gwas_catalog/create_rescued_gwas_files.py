#!/usr/bin/env python3
"""Generate derived rescued GWAS SSF files and their status manifest (issue #155).

Rescues 91 studies from OGS-00011 that failed the EAF orientation gate:
- 58 studies where correlation with comparison traits confirmed alleles are swapped:
  keeps both allele columns and `hm_code` exactly as the raw file has them (so
  `other_allele` stays the reference, which opengwasdb#381 reads for indels) and
  rewrites the statistics instead (`SwapStatistics`; issue #455).
- 33 studies where correlation confirmed alleles/beta are correct and only frequency was inverted:
  transforms `effect_allele_frequency` to `1.0 - EAF`.

Writes derived files under:
  /data/opengwasdb/derived/ebi-gwas-catalog/<bucket>/<accession>/harmonised/
And status manifest:
  /data/opengwasdb/derived/ebi-gwas-catalog/eur-hybrid-rescue-orientation-manifest.tsv

Run with:
  pixi run python3 -m manifest_generation.providers.gwas_catalog.create_rescued_gwas_files \
      [--only swap_alleles] [--derived-base DIR --manifest-out FILE]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import math
import os
import shutil
import time
from decimal import Decimal, InvalidOperation
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from manifest_generation import REPO_ROOT

SUMMARY_TSV = REPO_ROOT / "stores/OGS-00011/sidecars/allele-check/summary.tsv"
SOURCE_READINESS_TSV = REPO_ROOT / "stores/OGS-00011/sidecars/source_readiness.tsv"
DERIVED_BASE = Path("/data/opengwasdb/derived/ebi-gwas-catalog")
MANIFEST_OUT = DERIVED_BASE / "eur-hybrid-rescue-orientation-manifest.tsv"

MANIFEST_COLUMNS = [
    "analysis_id",
    "publication_pmid",
    "trait",
    "study_design",
    "sample_size",
    "status",
    "data_url",
    "yaml_url",
    "data_file",
    "yaml_file",
    "data_bytes",
    "yaml_bytes",
    "sha256",
    "seconds",
    "error",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


MISSING = frozenset({"", "NA"})


def _is_missing(value: str) -> bool:
    return value.strip() in MISSING


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"unreadable statistic {value!r}") from None


def _negate(value: str) -> str:
    """Negate a decimal string without touching its digits; missing stays missing."""
    if _is_missing(value):
        return value
    number = _decimal(value)
    if not number.is_finite():
        raise ValueError(f"cannot negate non-finite statistic {value!r}")
    if number == 0:
        return value
    v = value.strip()
    return v[1:] if v.startswith("-") else ("-" + v.lstrip("+"))


def _complement(value: str) -> str:
    """1 - value, exactly (decimal arithmetic); missing stays missing."""
    if _is_missing(value):
        return value
    number = _decimal(value)
    if not number.is_finite() or not Decimal(0) <= number <= Decimal(1):
        raise ValueError(f"frequency {value!r} is not in [0, 1]")
    return format(Decimal(1) - number, "f")


def _invert(value: str) -> str:
    """1 / value for an odds-ratio-scale statistic; missing stays missing."""
    if _is_missing(value):
        return value
    number = float(_decimal(value))
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"cannot invert odds-ratio-scale statistic {value!r}")
    return repr(1.0 / number)


def _flip_direction(value: str) -> str:
    """Exchange `+` and `-` in a per-cohort direction string (`?`/`0` stay)."""
    return value.translate(str.maketrans("+-", "-+"))


class SwapStatistics:
    """Express a GWAS-SSF row's statistics on the other allele, labels untouched.

    Every statistic that depends on which allele is the effect allele:

    * negated: `beta`, `z`, `Stat` (the signed score statistic; `Var` is not);
    * inverted: `odds_ratio`;
    * `ci_lower`/`ci_upper` alongside an `odds_ratio` are on its scale:
      new lower = 1 / old upper, new upper = 1 / old lower;
    * complemented: `effect_allele_frequency`;
    * `MinFreq`/`MaxFreq`: new min = 1 - old max, new max = 1 - old min;
    * `Direction`: `+` and `-` exchanged.

    Allele columns, `hm_code`, `ref_allele` (a label naming the raw allele
    roles, which stay) and every other column pass through as they are.
    """

    NEGATED = ("beta", "z", "Stat")

    def __init__(self, hdr: list[str]) -> None:
        self.hdr = hdr
        self.idx = {name: i for i, name in enumerate(hdr)}
        for required in ("effect_allele", "other_allele"):
            if required not in self.idx:
                raise ValueError(f"no {required} column")
        has_ci = "ci_lower" in self.idx or "ci_upper" in self.idx
        if has_ci and "odds_ratio" not in self.idx:
            raise ValueError("ci_lower/ci_upper without odds_ratio: scale unknown, no clear transformation")
        if ("ci_lower" in self.idx) != ("ci_upper" in self.idx):
            raise ValueError("only one of ci_lower/ci_upper present")
        if ("MinFreq" in self.idx) != ("MaxFreq" in self.idx):
            raise ValueError("only one of MinFreq/MaxFreq present")
        self.width = len(hdr)

    def row(self, parts: list[str]) -> list[str]:
        if len(parts) != self.width:
            raise ValueError(f"row has {len(parts)} fields, header {self.width}")
        out = list(parts)
        i = self.idx
        for name in self.NEGATED:
            if name in i:
                out[i[name]] = _negate(parts[i[name]])
        if "odds_ratio" in i:
            out[i["odds_ratio"]] = _invert(parts[i["odds_ratio"]])
        if "ci_lower" in i:
            out[i["ci_lower"]] = _invert(parts[i["ci_upper"]])
            out[i["ci_upper"]] = _invert(parts[i["ci_lower"]])
        if "effect_allele_frequency" in i:
            out[i["effect_allele_frequency"]] = _complement(parts[i["effect_allele_frequency"]])
        if "MinFreq" in i:
            out[i["MinFreq"]] = _complement(parts[i["MaxFreq"]])
            out[i["MaxFreq"]] = _complement(parts[i["MinFreq"]])
        if "Direction" in i:
            out[i["Direction"]] = _flip_direction(parts[i["Direction"]])
        return out


def rescue_file(raw_path: Path, out_path: Path, recommendation: str) -> None:
    """Write the rescued copy of one raw GWAS-SSF file (seam for tests)."""
    with gzip.open(raw_path, "rt", encoding="utf-8", errors="replace") as f_in, \
         gzip.open(out_path, "wt", encoding="utf-8") as f_out:
        header_line = f_in.readline()
        hdr = header_line.rstrip("\r\n").split("\t")
        f_out.write("\t".join(hdr) + "\n")

        if recommendation == "swap_alleles":
            swap = SwapStatistics(hdr)
            for line in f_in:
                parts = line.rstrip("\r\n").split("\t")
                f_out.write("\t".join(swap.row(parts)) + "\n")

        elif recommendation == "flip_frequency":
            eaf_idx = hdr.index("effect_allele_frequency")
            for line in f_in:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) > eaf_idx:
                    val_str = parts[eaf_idx].strip()
                    try:
                        val = float(val_str)
                        if 0.0 <= val <= 1.0:
                            # Format with consistent precision
                            parts[eaf_idx] = f"{1.0 - val:.6g}"
                    except (ValueError, TypeError):
                        pass
                f_out.write("\t".join(parts) + "\n")


def process_study(row: dict[str, str], recommendation: str, write_base: Path = DERIVED_BASE) -> dict[str, str]:
    started = time.time()
    aid = row["analysis_id"]
    raw_data_file = Path(row["data_file"])
    raw_yaml_file = Path(row["yaml_file"])

    # Mirror path structure: <bucket>/<accession>/harmonised/<data_name>
    data_name = raw_data_file.name
    yaml_name = raw_yaml_file.name
    rel_path = raw_data_file.relative_to("/data/opengwasdb/raw/ebi-gwas-catalog")
    # Files are written under `write_base`, but the manifest always names the
    # canonical DERIVED_BASE path, so a manifest built in scratch is installable.
    out_data_path = write_base / rel_path
    out_yaml_path = out_data_path.parent / yaml_name
    canonical_data_path = DERIVED_BASE / rel_path
    canonical_yaml_path = canonical_data_path.parent / yaml_name

    out_data_path.parent.mkdir(parents=True, exist_ok=True)

    # Copy yaml sidecar
    if raw_yaml_file.exists():
        shutil.copy2(raw_yaml_file, out_yaml_path)

    # Process data file via temporary sibling
    part_path = out_data_path.parent / f"{out_data_path.name}.part"
    rescue_file(raw_data_file, part_path, recommendation)
    part_path.replace(out_data_path)

    data_bytes = out_data_path.stat().st_size
    yaml_bytes = out_yaml_path.stat().st_size if out_yaml_path.exists() else 0
    checksum = sha256_file(out_data_path)
    elapsed = time.time() - started

    return {
        "analysis_id": aid,
        "publication_pmid": row.get("publication_pmid", ""),
        "trait": row.get("trait", ""),
        "study_design": row.get("study_design", ""),
        "sample_size": row.get("sample_size", ""),
        "status": "already_present",
        "data_url": row.get("data_url", ""),
        "yaml_url": row.get("yaml_url", ""),
        "data_file": str(canonical_data_path),
        "yaml_file": str(canonical_yaml_path),
        "data_bytes": str(data_bytes),
        "yaml_bytes": str(yaml_bytes),
        "sha256": checksum,
        "seconds": f"{elapsed:.1f}",
        "error": f"rescued via {recommendation}",
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--derived-base", type=Path, default=DERIVED_BASE,
                        help="where the rescued files are written (default: the canonical derived tree)")
    parser.add_argument("--manifest-out", type=Path, default=MANIFEST_OUT)
    parser.add_argument("--only", choices=("swap_alleles", "flip_frequency"),
                        help="regenerate one recommendation only; the other's rows are kept from the "
                             "existing manifest (--base-manifest)")
    parser.add_argument("--base-manifest", type=Path, default=MANIFEST_OUT,
                        help="manifest whose rows a partial run keeps for the studies it does not touch")
    args = parser.parse_args(argv)

    # 1. Read targets from summary.tsv
    with open(SUMMARY_TSV, "r", encoding="utf-8") as f:
        targets = {
            r["analysis_id"]: r["recommendation"]
            for r in csv.DictReader(f, delimiter="\t")
            if r["gate_reason"] == "eaf_orientation" and r["recommendation"] in ("swap_alleles", "flip_frequency")
        }
    all_targets = dict(targets)
    if args.only:
        targets = {k: v for k, v in targets.items() if v == args.only}

    print(f"Target studies to rescue: {len(targets)} (swap_alleles: {sum(v == 'swap_alleles' for v in targets.values())}, flip_frequency: {sum(v == 'flip_frequency' for v in targets.values())})")

    # 2. Read source readiness rows
    with open(SOURCE_READINESS_TSV, "r", encoding="utf-8") as f:
        source_rows = {
            r["analysis_id"]: r
            for r in csv.DictReader(f, delimiter="\t")
            if r["analysis_id"] in targets
        }

    # 3. Process studies in parallel
    results: list[dict[str, str]] = []
    max_workers = min(32, len(targets))
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(process_study, source_rows[aid], rec, args.derived_base): aid
            for aid, rec in targets.items()
        }
        for i, future in enumerate(as_completed(futures), start=1):
            res = future.result()
            results.append(res)
            print(f"[{i}/{len(targets)}] {res['analysis_id']} {res['error']} ({res['data_bytes']} bytes in {res['seconds']}s)", flush=True)

    # 4. A partial run keeps the other studies' rows exactly as they were.
    if args.only:
        with open(args.base_manifest, "r", encoding="utf-8", newline="") as f:
            kept = [r for r in csv.DictReader(f, delimiter="\t") if r["analysis_id"] not in targets]
        unknown = {r["analysis_id"] for r in kept} - set(all_targets)
        missing = set(all_targets) - set(targets) - {r["analysis_id"] for r in kept}
        if unknown or missing:
            raise SystemExit(f"base manifest disagrees with summary.tsv: unknown={sorted(unknown)} missing={sorted(missing)}")
        results.extend(kept)

    # 5. Write manifest sorted by analysis_id
    results.sort(key=lambda r: r["analysis_id"])
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.manifest_out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(results)

    print(f"Successfully wrote {len(results)} rows to {args.manifest_out}")


if __name__ == "__main__":
    main()
