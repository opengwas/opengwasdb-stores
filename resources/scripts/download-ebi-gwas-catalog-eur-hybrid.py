#!/usr/bin/env python3
"""Download raw EBI GWAS Catalog harmonised GRCh38 files for eur-hybrid.

The GWAS Catalog stores harmonised summary statistics under 1000-accession
buckets, but filenames are not completely uniform: many accessions use the
canonical ``<GCST>.h.tsv.gz`` name while some include PMID/EFO prefixes.  Which
file an accession actually publishes is read from EBI's own ``harmonised_list.txt``
index, falling back to a retrying FTP probe only for accessions the index omits
-- neither source is complete on its own, and a bare probe under concurrency is
throttled into looking like absence.  This
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
from typing import Iterable, Mapping
from urllib.parse import urljoin

from opengwasdb.readers.effect_source import (
    EffectSourceKind,
    resolve_effect_source,
    resolve_sample_size_column,
)

DEFAULT_CANDIDATES = "resources/data/derived/store-candidates-analyses.tsv"
DEFAULT_DEST = "/data/opengwasdb/raw/ebi-gwas-catalog"
DEFAULT_BASE_URL = "https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics"
#: EBI's own index of every harmonised GRCh38 file it publishes, one path per
#: line. Resolving names from it is not an optimisation: probing the FTP per
#: accession is throttled under concurrency, and a throttled probe is
#: indistinguishable from "this accession has no harmonised file" -- which is
#: how 161 accessions whose files were already on disk were recorded as
#: missing (issue #151).
DEFAULT_HARMONISED_INDEX = f"{DEFAULT_BASE_URL}/harmonised_list.txt"
DEFAULT_STORE_KEY = "hybrid__European"
REQUIRED_CORE_COLUMNS = {
    "chromosome",
    "base_pair_location",
    "effect_allele",
    "other_allele",
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
    parser.add_argument("--harmonised-index", default=DEFAULT_HARMONISED_INDEX, help="URL or local path of EBI's harmonised_list.txt")
    parser.add_argument("--store-key", default=DEFAULT_STORE_KEY)
    parser.add_argument("--workers", type=int, default=4, help="parallel downloads; keep modest for EBI")
    parser.add_argument("--limit", type=int, help="download only first N selected accessions")
    parser.add_argument("--accessions", help="comma-separated GCST accessions to download")
    parser.add_argument("--accessions-file", help="file of GCST accessions, one per line; blank lines and # comments ignored")
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
    """Fetch one file, distinguishing "upstream has no such file" from "the transfer failed".

    The two are different facts about a release input and must not share a
    status: a 404 is upstream's settled answer, while a refused connection is
    this run's problem and is worth retrying (issue #151).
    """
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
        # curl does not treat a refused connection as retryable by default, so
        # a server dropping connections under load looked like a hard failure.
        "--retry-connrefused",
        "--connect-timeout", "30",
        "--max-time", str(timeout),
        "--continue-at", "-",
        "--write-out", "%{http_code}",
        "--output", str(part),
        url,
    ]
    proc = run_curl(cmd, capture=True)
    if proc.returncode != 0:
        if (proc.stdout or "").strip() == "404":
            part.unlink(missing_ok=True)
            return "absent", 0, "upstream serves HTTP 404 for this file"
        return "failed", 0, (proc.stderr or "curl failed").strip()
    if not part.exists() or part.stat().st_size == 0:
        return "failed", 0, "download produced no bytes"
    os.replace(part, path)
    return "downloaded", path.stat().st_size, ""


def load_harmonised_index(source: str, timeout: int, retries: int) -> dict[str, list[str]]:
    """Read EBI's harmonised file index into ``accession -> [filename, ...]``.

    Every line is one published harmonised GRCh38 ``.h.tsv.gz`` path, so an
    accession absent from the index has no harmonised GRCh38 file upstream --
    a fact, rather than a lookup that may simply have been throttled.
    """
    if "://" in source:
        proc = run_curl(
            ["--retry", str(retries), "--retry-delay", "10", "--connect-timeout", "30",
             "--max-time", str(timeout), source],
            capture=True,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise SystemExit(
                f"could not fetch harmonised index {source}: {(proc.stderr or '').strip()}"
            )
        text = proc.stdout
    else:
        path = Path(source)
        if not path.is_file():
            raise SystemExit(f"--harmonised-index not found: {path}")
        text = path.read_text(encoding="utf-8")

    index: dict[str, list[str]] = {}
    pattern = re.compile(r"/(GCST\d+)/harmonised/([^/]+\.h\.tsv\.gz)$")
    for line in text.splitlines():
        match = pattern.search(line.strip())
        if match:
            index.setdefault(match.group(1), []).append(match.group(2))
    if not index:
        raise SystemExit(f"harmonised index {source} yielded no accessions")
    return index


def _remote_files(base_url: str, accession: str, data_name: str) -> RemoteFiles:
    directory = harmonised_dir_url(base_url, accession)
    yaml_name = f"{data_name}-meta.yaml"
    return RemoteFiles(
        data_url=urljoin(directory, data_name),
        yaml_url=urljoin(directory, yaml_name),
        data_name=data_name,
        yaml_name=yaml_name,
    )


def probe_remote_files(base_url: str, accession: str, timeout: int, retries: int) -> RemoteFiles | None:
    """Resolve an accession's harmonised filenames by asking the FTP directly.

    Only reached for accessions the index does not list. Every request carries
    retries: without them a throttled response is indistinguishable from an
    accession that publishes no harmonised file, which is precisely how ready
    files already on disk were recorded as missing (issue #151).

    The retries deliberately exclude ``--retry-all-errors``. curl's default
    retry set is the transient one (timeouts, 408, 429, 5xx); a 404 is a
    definitive answer about a file that does not exist and retrying it would
    spend the whole retry budget per absent accession.
    """
    retry = ["--retry", str(retries), "--retry-delay", "5",
             "--retry-max-time", str(timeout),
             "--connect-timeout", "30", "--max-time", str(timeout)]
    directory = harmonised_dir_url(base_url, accession)
    canonical = f"{accession}.h.tsv.gz"

    # Probe the data file, never the sidecar: accessions publish an orphan
    # `-meta.yaml` with no association file beside it, so the sidecar's presence
    # does not imply the data file's.
    probe = run_curl([*retry, "--head", urljoin(directory, canonical)], capture=True)
    if probe.returncode == 0:
        return _remote_files(base_url, accession, canonical)

    listing = run_curl([*retry, directory], capture=True)
    if listing.returncode != 0 or not listing.stdout:
        return None
    parser = LinkParser()
    parser.feed(listing.stdout)
    data_names = sorted(
        link for link in parser.links
        if link.endswith(".h.tsv.gz") and "Build37" not in link
    )
    if not data_names:
        return None
    return _remote_files(
        base_url, accession, next((n for n in data_names if accession in n), data_names[0])
    )


def resolve_remote_files(
    index: Mapping[str, list[str]], base_url: str, accession: str, timeout: int, retries: int
) -> RemoteFiles | None:
    names = index.get(accession)
    if not names:
        # The index is authoritative but not exhaustive: accessions whose
        # harmonised files serve 200 are absent from it, so absence justifies a
        # probe rather than concluding the file does not exist.
        return probe_remote_files(base_url, accession, timeout, retries)
    # Deterministic choice for the accessions publishing more than one
    # harmonised file: canonical name, then an accession-bearing name, then the
    # lowest sorted name.
    ordered = sorted(names)
    canonical = f"{accession}.h.tsv.gz"
    if canonical in ordered:
        data_name = canonical
    else:
        data_name = next((name for name in ordered if accession in name), ordered[0])
    return _remote_files(base_url, accession, data_name)


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


def ssf_header_ok(path: Path, study_design: str = "") -> tuple[bool, str]:
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            line = fh.readline()
            if not line:
                return False, "empty file: no header line"
            header = [c.strip() for c in line.rstrip("\r\n").split("\t")]
    except Exception as exc:  # noqa: BLE001 - status manifest should capture exact exception
        return False, f"cannot read gzip header: {exc}"

    missing_core = sorted(REQUIRED_CORE_COLUMNS.difference(header))
    if missing_core:
        return False, "missing required GWAS-SSF columns: " + ", ".join(missing_core)

    try:
        effect_source = resolve_effect_source(header)
    except ValueError as exc:
        return False, f"invalid effect column in header: {exc}"

    if effect_source is None:
        return False, "missing required GWAS-SSF effect column (expected beta, odds_ratio, or z_score)"

    if effect_source.kind in (EffectSourceKind.BETA, EffectSourceKind.ODDS_RATIO):
        if "standard_error" not in header:
            return False, "missing required GWAS-SSF columns: standard_error"
    elif effect_source.kind == EffectSourceKind.Z_SCORE:
        if study_design and study_design in ("case-control", "binary_trait"):
            return False, "case-control analysis cannot derive effect from z-score"
        sample_size_col = resolve_sample_size_column(header)
        if sample_size_col is None:
            return False, "z_score effect requires sample-size column ('n' or 'N')"
        if not ("effect_allele_frequency" in header or "hm_effect_allele_frequency" in header):
            return False, "z_score effect requires allele-frequency column ('effect_allele_frequency' or 'hm_effect_allele_frequency')"

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


def download_candidate(
    candidate: Candidate, args: argparse.Namespace, index: Mapping[str, list[str]]
) -> dict[str, str]:
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
        remote = resolve_remote_files(
            index, args.base_url, candidate.accession, args.timeout, args.retries
        )
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
        if yaml_status == "absent":
            # A local path column names a file that is on disk. Nothing was
            # written here, so the resolved names stay in the URL columns only.
            row["status"] = "missing_remote_harmonised_yaml"
            row["data_file"] = ""
            row["yaml_file"] = ""
            row["error"] = error
            return row
        if error:
            row["status"] = "yaml_failed"
            row["error"] = error
            return row
        ok, error = parse_yaml_gate(yaml_path)
        if not ok:
            # The gate runs before the association file is fetched, so no data
            # file was written and the row must not claim a path to one.
            row["status"] = "metadata_rejected"
            row["data_file"] = ""
            row["error"] = error
            return row

        data_status, data_bytes, error = curl_download(remote.data_url, data_path, args.timeout, args.retries, args.force)
        if data_status == "absent":
            # A resolved name whose data file 404s: the accession publishes an
            # orphan `-meta.yaml`, or the harmonised index still lists a file
            # upstream has withdrawn. Neither is a transfer this run can retry.
            row["status"] = "data_absent_upstream"
            row["data_file"] = ""
            row["error"] = error
            return row
        if error:
            row["status"] = "data_failed"
            row["error"] = error
            return row
        if not args.skip_header_check:
            ok, error = ssf_header_ok(data_path, candidate.study_design)
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


def _selected_accessions(args: argparse.Namespace) -> set[str] | None:
    selected: set[str] = set()
    if args.accessions:
        selected.update(part.strip() for part in args.accessions.split(",") if part.strip())
    if args.accessions_file:
        path = Path(args.accessions_file)
        if not path.is_file():
            raise SystemExit(f"--accessions-file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                selected.add(line)
        if not selected:
            raise SystemExit(f"--accessions-file selected nothing: {path}")
    return selected or None


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    accessions = _selected_accessions(args)
    candidates = read_candidates(Path(args.candidates), args.store_key, accessions, args.limit)
    if not candidates:
        raise SystemExit("no candidates selected")

    manifest_path = Path(args.manifest) if args.manifest else Path(args.dest) / "eur-hybrid-download-manifest.tsv"
    index = load_harmonised_index(args.harmonised_index, args.timeout, args.retries)
    print(
        f"Selected {len(candidates)} {args.store_key} candidate(s); "
        f"destination={args.dest}; workers={args.workers}; manifest={manifest_path}; "
        f"harmonised index={args.harmonised_index} ({len(index)} accessions)",
        flush=True,
    )

    results: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_candidate = {
            pool.submit(download_candidate, candidate, args, index): candidate
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
            # Frequent checkpointing matters for multi-day download batches.
            write_manifest(manifest_path, sorted(results, key=lambda r: r["analysis_id"]))

    write_manifest(manifest_path, sorted(results, key=lambda r: r["analysis_id"]))
    print("Done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    failures = sum(v for k, v in counts.items() if k not in {"ok", "already_present", "dry_run"})
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
