#!/usr/bin/env python3
"""Generate derived rescued GWAS SSF files and their status manifest (issue #155).

Rescues 91 studies from OGS-00011 that failed the EAF orientation gate:
- 58 studies where correlation with comparison traits confirmed alleles are swapped:
  keeps both allele columns and `hm_code` exactly as the raw file has them (so
  `other_allele` stays the reference, which opengwasdb#381 reads for indels) and
  rewrites the harmonised statistics instead (unharmonised study columns stay as raw) (`SwapStatistics`; issue #455).
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
from decimal import Decimal, InvalidOperation, localcontext
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


#: swap_alleles studies excluded from the rescue for good (owner, 10 Nov 2026; review of #455).
#: GCST90275159 and GCST90275160 carry dummy effects by their own yaml: `beta` is -9 or 9 by
#: hm_code and the standard error is 1000, so their swap_alleles verdict (r = -0.445 over 62
#: variants) rests on no real effects. They are never regenerated or replaced; the rescue
#: manifest marks them `excluded` with no file, checksum or size, so no reader takes them as
#: rescued. Their current derived copies stay on disk, unused.
EXCLUDED_SWAP_STUDIES = frozenset({"GCST90275159", "GCST90275160"})
EXCLUDED_STATUS = "excluded"
EXCLUDED_REASON = (
    "excluded from rescue (owner, 10 Nov 2026): dummy beta (-9/9 by hm_code) and SE (1000) per its "
    "yaml, so the swap_alleles verdict (r = -0.445 over 62 variants) rests on no real effects"
)


def excluded_row(row: dict[str, str]) -> dict[str, str]:
    """The manifest row of an excluded study: described, never pointing at a file."""
    out = {c: "" for c in MANIFEST_COLUMNS}
    for c in ("analysis_id", "publication_pmid", "trait", "study_design", "sample_size", "data_url", "yaml_url"):
        out[c] = row.get(c, "")
    out["status"] = EXCLUDED_STATUS
    out["error"] = EXCLUDED_REASON
    return out


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
    """1 - value, exactly: the precision follows the input's own digits."""
    if _is_missing(value):
        return value
    number = _decimal(value)
    if not number.is_finite() or not Decimal(0) <= number <= Decimal(1):
        raise ValueError(f"frequency {value!r} is not in [0, 1]")
    _, digits, exponent = number.as_tuple()
    with localcontext() as ctx:
        ctx.prec = len(digits) + abs(int(exponent)) + 5
        return format(Decimal(1) - number, "f")


def _invert(value: str) -> str:
    """1 / value for an odds-ratio-scale statistic; missing stays missing."""
    if _is_missing(value):
        return value
    number = float(_decimal(value))
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"cannot invert odds-ratio-scale statistic {value!r}")
    return repr(1.0 / number)


class SwapStatistics:
    """Express a GWAS-SSF row's harmonised statistics on the other allele, labels untouched.

    The EBI harmoniser flips `beta`, `odds_ratio`, its interval and
    `effect_allele_frequency` along with the alleles (hm_code 11/6/13). Those,
    and only those, are rewritten:

    * negated: `beta`;
    * inverted: `odds_ratio`; `ci_lower`/`ci_upper` alongside it are on its scale:
      new lower = 1 / old upper, new upper = 1 / old lower;
    * complemented: `effect_allele_frequency`.

    Allele columns, `hm_code` and the direction-free columns pass through as
    they are. So do the **unharmonised study columns** (`MinFreq`, `MaxFreq`,
    `Direction`, `FreqSE`): the harmoniser never flipped them, so they follow
    the study's own effect allele on every row whatever its `hm_code`, and one
    transformation would put them on the wrong allele for the rows it did not
    flip (in raw GCST90446775, `[MinFreq, MaxFreq]` brackets the EAF on
    4,503,340 of 4,503,368 rows with hm_code 10/5/12 but 26,727 of 3,756,738
    with 11/6/13; `Direction` agrees with `beta` in 99.98% and 0.01% of the
    same rows). A column in none of the three lists refuses the file: it might
    depend on the effect allele.
    """

    TRANSFORMED = frozenset({"beta", "odds_ratio", "ci_lower", "ci_upper", "effect_allele_frequency"})
    DIRECTION_FREE = frozenset({
        "chromosome", "base_pair_location", "effect_allele", "other_allele", "standard_error",
        "p_value", "variant_id", "variant_id_when_present", "n", "hm_coordinate_conversion",
        "hm_code", "rsid", "rs_id", "locus_id", "info",
    })
    UNHARMONISED = frozenset({"MinFreq", "MaxFreq", "Direction", "FreqSE"})

    def __init__(self, hdr: list[str]) -> None:
        self.hdr = hdr
        self.idx = {name: i for i, name in enumerate(hdr)}
        for required in ("effect_allele", "other_allele"):
            if required not in self.idx:
                raise ValueError(f"no {required} column")
        known = self.TRANSFORMED | self.DIRECTION_FREE | self.UNHARMONISED
        unknown = [h for h in hdr if h not in known]
        if unknown:
            raise ValueError(
                f"columns {unknown} are not classified as harmonised, direction-free or "
                "unharmonised: no clear transformation"
            )
        has_ci = "ci_lower" in self.idx or "ci_upper" in self.idx
        if has_ci and "odds_ratio" not in self.idx:
            raise ValueError("ci_lower/ci_upper without odds_ratio: scale unknown, no clear transformation")
        if ("ci_lower" in self.idx) != ("ci_upper" in self.idx):
            raise ValueError("only one of ci_lower/ci_upper present")
        self.width = len(hdr)

    def row(self, parts: list[str]) -> list[str]:
        if len(parts) != self.width:
            raise ValueError(f"row has {len(parts)} fields, header {self.width}")
        out = list(parts)
        i = self.idx
        if "beta" in i:
            out[i["beta"]] = _negate(parts[i["beta"]])
        if "odds_ratio" in i:
            out[i["odds_ratio"]] = _invert(parts[i["odds_ratio"]])
        if "ci_lower" in i:
            out[i["ci_lower"]] = _invert(parts[i["ci_upper"]])
            out[i["ci_upper"]] = _invert(parts[i["ci_lower"]])
        if "effect_allele_frequency" in i:
            out[i["effect_allele_frequency"]] = _complement(parts[i["effect_allele_frequency"]])
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
    if args.derived_base != DERIVED_BASE and args.manifest_out == MANIFEST_OUT:
        parser.error("--derived-base needs --manifest-out: a scratch run must not overwrite the live manifest")

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
    excluded = sorted(k for k in all_targets if k in EXCLUDED_SWAP_STUDIES and all_targets[k] == "swap_alleles")
    targets = {k: v for k, v in targets.items() if k not in excluded}
    if excluded:
        print(f"Excluded from the rescue (dummy effects, owner 10 Nov): {', '.join(excluded)}")

    print(f"Target studies to rescue: {len(targets)} (swap_alleles: {sum(v == 'swap_alleles' for v in targets.values())}, flip_frequency: {sum(v == 'flip_frequency' for v in targets.values())})")

    # 2. Read source readiness rows
    with open(SOURCE_READINESS_TSV, "r", encoding="utf-8") as f:
        source_rows = {
            r["analysis_id"]: r
            for r in csv.DictReader(f, delimiter="\t")
            if r["analysis_id"] in targets or r["analysis_id"] in excluded
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
    results.extend(excluded_row(source_rows[aid]) for aid in excluded)
    if args.only:
        with open(args.base_manifest, "r", encoding="utf-8", newline="") as f:
            kept = [r for r in csv.DictReader(f, delimiter="\t")
                    if r["analysis_id"] not in targets and r["analysis_id"] not in excluded]
        unknown = {r["analysis_id"] for r in kept} - set(all_targets)
        missing = set(all_targets) - set(targets) - set(excluded) - {r["analysis_id"] for r in kept}
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
