#!/usr/bin/env python3
"""Download raw EBI GWAS Catalog harmonised GRCh38 files for eur-hybrid.

The GWAS Catalog stores harmonised summary statistics under 1000-accession
buckets, but filenames are not completely uniform: many accessions use the
canonical ``<GCST>.h.tsv.gz`` name while some include PMID/EFO prefixes.  This
script downloads exactly one harmonised GRCh38 data file plus its corresponding
``*-meta.yaml`` for each candidate analysis in the eur-hybrid pool, preserving a
small mirror of the EBI layout under the destination directory.

Default input is the derived candidate table whose ``store_key`` defines the
``hybrid__European`` store.  The script is resumable: completed files are
skipped, ``.part`` files are resumed with curl, and every run writes a TSV
status manifest.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html.parser
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

DEFAULT_CANDIDATES = "resources/data/derived/store-candidates-analyses.tsv"
DEFAULT_DEST = "/data/opengwasdb/raw/ebi-gwas-catalog"
DEFAULT_BASE_URL = "https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics"
DEFAULT_STORE_KEY = "hybrid__European"
REQUIRED_SSF_COLUMNS = {
    "chromosome",
    "base_pair_location",
    "effect_allele",
    "other_allele",
    "beta",
    "standard_error",
}


class LinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.links.append(value)


@dataclass(frozen=True)
class Candidate:
    accession: str
    pmid: str
    trait: str
    study_design: str
    sample_size: str


@dataclass(frozen=True)
class RemoteFiles:
    data_url: str
    yaml_url: str
    data_name: str
    yaml_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--dest", default=DEFAULT_DEST)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--store-key", default=DEFAULT_STORE_KEY)
    parser.add_argument("--workers", type=int, default=4, help="parallel downloads; keep modest for EBI")
    parser.add_argument("--limit", type=int, help="download only first N selected accessions")
    parser.add_argument("--accessions", help="comma-separated GCST accessions to download")
    parser.add_argument("--timeout", type=int, default=1800, help="curl max time per file in seconds")
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--manifest", help="status TSV path (default: <dest>/eur-hybrid-download-manifest.tsv)")
    parser.add_argument("--dry-run", action="store_true", help="resolve candidates but do not download")
    parser.add_argument("--force", action="store_true", help="re-download even when final files exist")
    parser.add_argument("--skip-header-check", action="store_true")
    return parser.parse_args()


def bucket_of(gcst: str) -> str:
    digits = gcst.removeprefix("GCST")
    n = int(digits)
    lo = ((n - 1) // 1000) * 1000 + 1
    width = len(digits)
    return f"GCST{lo:0{width}d}-GCST{lo + 999:0{width}d}"


def harmonised_dir_url(base_url: str, gcst: str) -> str:
    return f"{base_url.rstrip('/')}/{bucket_of(gcst)}/{gcst}/harmonised/"


def run_curl(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["curl", "--fail", "--location", "--show-error", "--silent", *args]
    return subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)


def curl_download(url: str, path: Path, timeout: int, retries: int, force: bool) -> tuple[str, int, str]:
    if path.exists() and path.stat().st_size > 0 and not force:
        return "skipped", path.stat().st_size, ""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    if force:
        part.unlink(missing_ok=True)
    cmd = [
        "--retry", str(retries),
        "--retry-delay", "10",
        "--retry-max-time", str(max(timeout, retries * 60)),
        "--connect-timeout", "30",
        "--max-time", str(timeout),
        "--continue-at", "-",
        "--output", str(part),
        url,
    ]
    proc = run_curl(cmd)
    if proc.returncode != 0:
        return "failed", 0, (proc.stderr or "curl failed").strip()
    if not part.exists() or part.stat().st_size == 0:
        return "failed", 0, "download produced no bytes"
    os.replace(part, path)
    return "downloaded", path.stat().st_size, ""


def list_remote_files(base_url: str, accession: str) -> RemoteFiles | None:
    directory = harmonised_dir_url(base_url, accession)
    canonical_data = f"{accession}.h.tsv.gz"
    canonical_yaml = f"{canonical_data}-meta.yaml"

    # Fast path for the canonical filenames used by most current harmonised SSF files.
    probe = run_curl(["--head", urljoin(directory, canonical_yaml)], capture=True)
    if probe.returncode == 0:
        return RemoteFiles(
            data_url=urljoin(directory, canonical_data),
            yaml_url=urljoin(directory, canonical_yaml),
            data_name=canonical_data,
            yaml_name=canonical_yaml,
        )

    index = run_curl([directory], capture=True)
    if index.returncode != 0 or not index.stdout:
        return None
    parser = LinkParser()
    parser.feed(index.stdout)
    yaml_names = sorted(
        link for link in parser.links
        if link.endswith(".h.tsv.gz-meta.yaml") and "Build37" not in link
    )
    if not yaml_names:
        return None
    # Prefer a file whose name contains the accession; otherwise take the first
    # harmonised metadata file in the accession directory.
    yaml_name = next((name for name in yaml_names if accession in name), yaml_names[0])
    data_name = yaml_name.removesuffix("-meta.yaml")
    return RemoteFiles(
        data_url=urljoin(directory, data_name),
        yaml_url=urljoin(directory, yaml_name),
        data_name=data_name,
        yaml_name=yaml_name,
    )


def read_candidates(path: Path, store_key: str, accessions: set[str] | None, limit: int | None) -> list[Candidate]:
    out: list[Candidate] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            accession = row.get("STUDY.ACCESSION", "")
            if row.get("store_key") != store_key:
                continue
            if accessions is not None and accession not in accessions:
                continue
            out.append(Candidate(
                accession=accession,
                pmid=row.get("PUBMED.ID", ""),
                trait=row.get("DISEASE.TRAIT", ""),
                study_design=row.get("study_design", ""),
                sample_size=row.get("sample_size", ""),
            ))
            if limit is not None and len(out) >= limit:
                break
    return out


def parse_yaml_gate(path: Path) -> tuple[bool, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    assembly = re.search(r"(?m)^genome_assembly:\s*([^\n#]+)", text)
    harmonised = re.search(r"(?m)^is_harmonised:\s*([^\n#]+)", text)
    if not assembly or assembly.group(1).strip() != "GRCh38":
        return False, f"metadata genome_assembly is not GRCh38 ({assembly.group(1).strip() if assembly else 'missing'})"
    if not harmonised or harmonised.group(1).strip().lower() != "true":
        return False, f"metadata is_harmonised is not true ({harmonised.group(1).strip() if harmonised else 'missing'})"
    return True, ""


def ssf_header_ok(path: Path) -> tuple[bool, str]:
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            header = fh.readline().rstrip("\n").split("\t")
    except Exception as exc:  # noqa: BLE001 - status manifest should capture exact exception
        return False, f"cannot read gzip header: {exc}"
    missing = sorted(REQUIRED_SSF_COLUMNS.difference(header))
    if missing:
        return False, "missing required GWAS-SSF columns: " + ", ".join(missing)
    return True, ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def local_paths(dest: Path, accession: str, remote: RemoteFiles) -> tuple[Path, Path]:
    directory = dest / bucket_of(accession) / accession / "harmonised"
    return directory / remote.data_name, directory / remote.yaml_name


def download_candidate(candidate: Candidate, args: argparse.Namespace) -> dict[str, str]:
    started = time.time()
    row = {
        "analysis_id": candidate.accession,
        "publication_pmid": candidate.pmid,
        "trait": candidate.trait,
        "study_design": candidate.study_design,
        "sample_size": candidate.sample_size,
        "status": "",
        "data_url": "",
        "yaml_url": "",
        "data_file": "",
        "yaml_file": "",
        "data_bytes": "",
        "yaml_bytes": "",
        "sha256": "",
        "seconds": "",
        "error": "",
    }
    try:
        remote = list_remote_files(args.base_url, candidate.accession)
        if remote is None:
            row["status"] = "missing_remote_harmonised_yaml"
            return row
        data_path, yaml_path = local_paths(Path(args.dest), candidate.accession, remote)
        row.update({
            "data_url": remote.data_url,
            "yaml_url": remote.yaml_url,
            "data_file": str(data_path),
            "yaml_file": str(yaml_path),
        })
        if args.dry_run:
            row["status"] = "dry_run"
            return row

        yaml_status, yaml_bytes, error = curl_download(remote.yaml_url, yaml_path, args.timeout, args.retries, args.force)
        if error:
            row["status"] = "yaml_failed"
            row["error"] = error
            return row
        ok, error = parse_yaml_gate(yaml_path)
        if not ok:
            row["status"] = "metadata_rejected"
            row["error"] = error
            return row

        data_status, data_bytes, error = curl_download(remote.data_url, data_path, args.timeout, args.retries, args.force)
        if error:
            row["status"] = "data_failed"
            row["error"] = error
            return row
        if not args.skip_header_check:
            ok, error = ssf_header_ok(data_path)
            if not ok:
                row["status"] = "header_rejected"
                row["error"] = error
                return row
        row["status"] = "ok" if "downloaded" in (yaml_status, data_status) else "already_present"
        row["data_bytes"] = str(data_bytes)
        row["yaml_bytes"] = str(yaml_bytes)
        row["sha256"] = sha256_file(data_path)
        return row
    except Exception as exc:  # noqa: BLE001 - keep long-running batch alive
        row["status"] = "error"
        row["error"] = repr(exc)
        return row
    finally:
        row["seconds"] = f"{time.time() - started:.1f}"


def write_manifest(path: Path, rows: Iterable[dict[str, str]]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "analysis_id", "publication_pmid", "trait", "study_design", "sample_size",
        "status", "data_url", "yaml_url", "data_file", "yaml_file", "data_bytes",
        "yaml_bytes", "sha256", "seconds", "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    accessions = set(args.accessions.split(",")) if args.accessions else None
    candidates = read_candidates(Path(args.candidates), args.store_key, accessions, args.limit)
    if not candidates:
        raise SystemExit("no candidates selected")

    manifest_path = Path(args.manifest) if args.manifest else Path(args.dest) / "eur-hybrid-download-manifest.tsv"
    print(
        f"Selected {len(candidates)} {args.store_key} candidate(s); "
        f"destination={args.dest}; workers={args.workers}; manifest={manifest_path}",
        flush=True,
    )

    results: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_candidate = {pool.submit(download_candidate, candidate, args): candidate for candidate in candidates}
        for i, future in enumerate(as_completed(future_to_candidate), start=1):
            result = future.result()
            results.append(result)
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            print(
                f"[{i}/{len(candidates)}] {result['analysis_id']} {result['status']} "
                f"{result['data_bytes'] or ''} {result['error']}",
                flush=True,
            )
            # Frequent checkpointing matters for multi-day download batches.
            write_manifest(manifest_path, sorted(results, key=lambda r: r["analysis_id"]))

    write_manifest(manifest_path, sorted(results, key=lambda r: r["analysis_id"]))
    print("Done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    failures = sum(v for k, v in counts.items() if k not in {"ok", "already_present", "dry_run"})
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
