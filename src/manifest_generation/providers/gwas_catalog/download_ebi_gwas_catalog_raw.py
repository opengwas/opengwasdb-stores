#!/usr/bin/env python3
"""Acquire the raw GWAS-SSF files EBI serves when no harmonised file exists.

EBI publishes two forms of a GWAS Catalog summary-statistics file:

* the raw ``<GCST>.tsv.gz`` (GWAS-SSF v1.0, source genome build declared in
  ``<GCST>.tsv.gz-meta.yaml``), and
* a harmonised ``harmonised/<GCST>.h.tsv.gz`` with ``hm_*`` columns.

For PMID 39024449 a large fraction of accessions are listed in EBI's
``harmonised_list.txt`` but publish only the raw file -- the ``harmonised/``
directory genuinely does not exist. The eur-hybrid downloader records those as
``missing_remote_harmonised_yaml`` and moves on, because that release admits
harmonised inputs only. This pass closes the acquisition gap: for every
accession the harmonised pass did *not* obtain, it downloads the raw pair into
the same mirror layout, so the study is fully on disk and a later harmonisation
step can migrate the raw files.

It is intentionally separate from the harmonised pass:

* it reads the harmonised pass's status manifest and only touches rows that are
  not ``ok``/``already_present``, so an accession is never fetched twice;
* it never changes the harmonised manifest or the ``hybrid__European`` policy;
* membership stays a Phase B decision -- this pass only makes the bytes
  available.

The raw meta yaml is fetched best-effort: a missing sidecar is recorded, not
fatal, because the association file is the artifact a migration needs. The
download is resumable exactly like the harmonised one (``.part`` files with
curl ``--continue-at``).
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

_SCRIPT = Path(__file__).with_name("download-ebi-gwas-catalog-eur-hybrid.py")
_spec = importlib.util.spec_from_file_location("ebi_harmonised_download", _SCRIPT)
assert _spec and _spec.loader
ebi = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ebi
_spec.loader.exec_module(ebi)

#: Harmonised-manifest statuses that already prove an accession has a local
#: association file; the raw pass must not re-fetch those.
HARMONISED_READY = {"ok", "already_present", "dry_run"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=ebi.DEFAULT_CANDIDATES)
    parser.add_argument("--store-key", default=ebi.DEFAULT_STORE_KEY)
    parser.add_argument("--dest", default=ebi.DEFAULT_DEST)
    parser.add_argument("--base-url", default=ebi.DEFAULT_BASE_URL)
    parser.add_argument("--harmonised-manifest", required=True,
                        help="status TSV written by the harmonised pass for the same --store-key")
    parser.add_argument("--manifest", required=True, help="raw-pass status TSV to write")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--retries", type=int, default=12)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def raw_remote_files(base_url: str, accession: str) -> tuple[str, str]:
    """Raw data + sidecar URLs. Unlike harmonised files these are deterministic:
    the raw file always sits directly in the accession directory."""
    directory = f"{base_url.rstrip('/')}/{ebi.bucket_of(accession)}/{accession}/"
    data_name = f"{accession}.tsv.gz"
    return urljoin(directory, data_name), urljoin(directory, f"{data_name}-meta.yaml")


def local_raw_paths(dest: Path, accession: str) -> tuple[Path, Path]:
    directory = dest / ebi.bucket_of(accession) / accession
    return directory / f"{accession}.tsv.gz", directory / f"{accession}.tsv.gz-meta.yaml"


def harmonised_data_present(dest: Path, accession: str) -> bool:
    directory = dest / ebi.bucket_of(accession) / accession / "harmonised"
    if not directory.is_dir():
        return False
    return any(p.is_file() and p.stat().st_size > 0 for p in directory.glob("*.h.tsv.gz"))


def parse_raw_yaml(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")

    def field(name: str) -> str:
        match = re.search(rf"(?m)^{name}:\s*([^\n#]+)", text)
        return match.group(1).strip() if match else ""

    return {
        "genome_assembly": field("genome_assembly"),
        "file_type": field("file_type"),
        "is_harmonised": field("is_harmonised"),
    }


def read_harmonised_manifest(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"--harmonised-manifest not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["analysis_id"]: row.get("status", "") for row in csv.DictReader(handle, delimiter="\t")}


def download_raw(
    candidate: "ebi.Candidate", harmonised_status: str, args: argparse.Namespace
) -> dict[str, str]:
    started = time.time()
    row = {
        "analysis_id": candidate.accession,
        "publication_pmid": candidate.pmid,
        "trait": candidate.trait,
        "study_design": candidate.study_design,
        "sample_size": candidate.sample_size,
        "harmonised_status": harmonised_status,
        "status": "",
        "data_url": "",
        "yaml_url": "",
        "data_file": "",
        "yaml_file": "",
        "data_bytes": "",
        "yaml_bytes": "",
        "sha256": "",
        "genome_assembly": "",
        "file_type": "",
        "is_harmonised": "",
        "seconds": "",
        "error": "",
    }
    try:
        # Never fetch raw bytes for an accession whose harmonised file the other
        # pass already obtained, including across re-runs.
        if harmonised_status in HARMONISED_READY or harmonised_data_present(Path(args.dest), candidate.accession):
            row["status"] = "skipped_harmonised"
            return row

        data_url, yaml_url = raw_remote_files(args.base_url, candidate.accession)
        data_path, yaml_path = local_raw_paths(Path(args.dest), candidate.accession)
        row.update({"data_url": data_url, "yaml_url": yaml_url,
                    "data_file": str(data_path), "yaml_file": str(yaml_path)})

        yaml_status, yaml_bytes, yaml_error = ebi.curl_download(
            yaml_url, yaml_path, args.timeout, args.retries, args.force
        )
        if yaml_status in {"skipped", "downloaded"}:
            meta = parse_raw_yaml(yaml_path)
            row.update(meta)
            row["yaml_bytes"] = str(yaml_bytes)
        else:
            # A missing sidecar is evidence, not a reason to drop the bytes: the
            # association file is what a migration consumes. Record it and try.
            row["error"] = yaml_error

        data_status, data_bytes, data_error = ebi.curl_download(
            data_url, data_path, args.timeout, args.retries, args.force
        )
        if data_status == "absent":
            row["status"] = "data_absent_upstream"
            row["error"] = data_error
            return row
        if data_error:
            row["status"] = "data_failed"
            row["error"] = data_error
            return row

        ok, header_error = ebi.ssf_header_ok(data_path, candidate.study_design)
        if not ok:
            row["status"] = "raw_header_rejected"
            row["error"] = header_error
            return row

        row["status"] = "ok" if "downloaded" in (data_status, yaml_status) else "already_present"
        row["data_bytes"] = str(data_bytes)
        row["sha256"] = ebi.sha256_file(data_path)
        return row
    except Exception as exc:  # noqa: BLE001 - keep a long batch alive
        row["status"] = "error"
        row["error"] = repr(exc)
        return row
    finally:
        row["seconds"] = f"{time.time() - started:.1f}"


def write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "analysis_id", "publication_pmid", "trait", "study_design", "sample_size",
        "harmonised_status", "status", "data_url", "yaml_url", "data_file", "yaml_file",
        "data_bytes", "yaml_bytes", "sha256", "genome_assembly", "file_type",
        "is_harmonised", "seconds", "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    harmonised = read_harmonised_manifest(Path(args.harmonised_manifest))
    candidates = ebi.read_candidates(Path(args.candidates), args.store_key, None, None)
    if not candidates:
        raise SystemExit("no candidates selected")

    manifest_path = Path(args.manifest)
    print(
        f"Raw fallback for {args.store_key}: {len(candidates)} candidate(s); "
        f"harmonised manifest={args.harmonised_manifest}; workers={args.workers}; "
        f"manifest={manifest_path}",
        flush=True,
    )

    results: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_candidate = {
            pool.submit(download_raw, candidate, harmonised.get(candidate.accession, ""), args): candidate
            for candidate in candidates
        }
        for i, future in enumerate(as_completed(future_to_candidate), start=1):
            result = future.result()
            results.append(result)
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            print(
                f"[{i}/{len(candidates)}] {result['analysis_id']} {result['status']} "
                f"{result['data_bytes'] or ''} {result['error']}",
                flush=True,
            )
            write_manifest(manifest_path, sorted(results, key=lambda r: r["analysis_id"]))

    write_manifest(manifest_path, sorted(results, key=lambda r: r["analysis_id"]))
    print("Done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    failures = sum(v for k, v in counts.items()
                   if k not in {"ok", "already_present", "skipped_harmonised"})
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
