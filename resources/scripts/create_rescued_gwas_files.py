#!/usr/bin/env python3
"""Generate derived rescued GWAS SSF files and their status manifest (issue #155).

Rescues 91 studies from OGS-00011 that failed the EAF orientation gate:
- 58 studies where correlation with comparison traits confirmed alleles are swapped:
  swaps `effect_allele` and `other_allele` values.
- 33 studies where correlation confirmed alleles/beta are correct and only frequency was inverted:
  transforms `effect_allele_frequency` to `1.0 - EAF`.

Writes derived files under:
  /data/opengwasdb/derived/ebi-gwas-catalog/<bucket>/<accession>/harmonised/
And status manifest:
  /data/opengwasdb/derived/ebi-gwas-catalog/eur-hybrid-rescue-orientation-manifest.tsv
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
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


def process_study(row: dict[str, str], recommendation: str) -> dict[str, str]:
    started = time.time()
    aid = row["analysis_id"]
    raw_data_file = Path(row["data_file"])
    raw_yaml_file = Path(row["yaml_file"])

    # Mirror path structure: <bucket>/<accession>/harmonised/<data_name>
    data_name = raw_data_file.name
    yaml_name = raw_yaml_file.name
    rel_path = raw_data_file.relative_to("/data/opengwasdb/raw/ebi-gwas-catalog")
    out_data_path = DERIVED_BASE / rel_path
    out_yaml_path = out_data_path.parent / yaml_name

    out_data_path.parent.mkdir(parents=True, exist_ok=True)

    # Copy yaml sidecar
    if raw_yaml_file.exists():
        shutil.copy2(raw_yaml_file, out_yaml_path)

    # Process data file via temporary sibling
    part_path = out_data_path.parent / f"{out_data_path.name}.part"
    with gzip.open(raw_data_file, "rt", encoding="utf-8", errors="replace") as f_in, \
         gzip.open(part_path, "wt", encoding="utf-8") as f_out:
        header_line = f_in.readline()
        hdr = header_line.rstrip("\r\n").split("\t")
        f_out.write("\t".join(hdr) + "\n")

        if recommendation == "swap_alleles":
            ea_idx = hdr.index("effect_allele")
            oa_idx = hdr.index("other_allele")
            for line in f_in:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) > max(ea_idx, oa_idx):
                    parts[ea_idx], parts[oa_idx] = parts[oa_idx], parts[ea_idx]
                f_out.write("\t".join(parts) + "\n")

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
        "data_file": str(out_data_path),
        "yaml_file": str(out_yaml_path),
        "data_bytes": str(data_bytes),
        "yaml_bytes": str(yaml_bytes),
        "sha256": checksum,
        "seconds": f"{elapsed:.1f}",
        "error": f"rescued via {recommendation}",
    }


def main() -> None:
    # 1. Read targets from summary.tsv
    with open(SUMMARY_TSV, "r", encoding="utf-8") as f:
        targets = {
            r["analysis_id"]: r["recommendation"]
            for r in csv.DictReader(f, delimiter="\t")
            if r["gate_reason"] == "eaf_orientation" and r["recommendation"] in ("swap_alleles", "flip_frequency")
        }

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
            pool.submit(process_study, source_rows[aid], rec): aid
            for aid, rec in targets.items()
        }
        for i, future in enumerate(as_completed(futures), start=1):
            res = future.result()
            results.append(res)
            print(f"[{i}/{len(targets)}] {res['analysis_id']} {res['error']} ({res['data_bytes']} bytes in {res['seconds']}s)", flush=True)

    # 4. Write manifest sorted by analysis_id
    results.sort(key=lambda r: r["analysis_id"])
    MANIFEST_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(results)

    print(f"Successfully wrote {len(results)} rows to {MANIFEST_OUT}")


if __name__ == "__main__":
    main()
