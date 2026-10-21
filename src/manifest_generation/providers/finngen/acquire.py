#!/usr/bin/env python3
"""Acquire FinnGen R13 endpoint files and write the shared Acquisition Manifest (#288, ADR 0036).

FinnGen publishes one summary-statistics file per endpoint and lists them, with
their HTTPS URLs, in ``finngen_R13_manifest.tsv``. This script fetches every
listed endpoint's file into one source directory -- resumable, idempotent, with
``.part`` files, HTTP Range requests and atomic promotion -- and records what it
produced as a canonical **Acquisition Manifest**
(``manifest_generation.acquisition_manifest``): per Analysis, the file's
location, size and sha256, and its URL, or the readiness status and error that
explain why it has no usable file. That is availability, never intent: the
endpoint manifest's metadata is the metadata adapter's
(``source_metadata_adapter``), and the two connector files meet at the shared
freeze.

A file already present is checksummed and kept, not re-fetched. An endpoint the
provider no longer serves is recorded as ``data_absent_upstream``; a failed
transfer as ``data_failed``. The manifest is written either way -- the absence
is evidence freeze needs -- and the run then exits non-zero, so a retry wrapper
re-runs it and every completed file is reused.

Beside the manifest this writes ``<manifest-stem>.meta.yaml``, the pass lineage
freeze checksums into the Source Inventory's provenance
(``acquisition_manifest.pass_lineage_path``): the provider manifest's path,
sha256, size and row count, the written manifest's sha256 and readiness counts,
and the run's settings.

A file whose bytes no longer match the digest the manifest at ``--out`` already
recorded for it is refused by Analysis and never re-hashed into a new manifest:
a recorded checksum is identity evidence, not a field a later run may overwrite.

Usage:
    python3 -m manifest_generation.providers.finngen.acquire \
        --manifest /data/opengwasdb/raw/finngen-r13/releases/r13-full/manifest/finngen_R13_manifest.tsv \
        --source-dir /data/opengwasdb/raw/finngen-r13/releases/r13-full/source \
        --out /data/opengwasdb/raw/finngen-r13/connectors/<date>/finngen-r13-full.acquisition-manifest.tsv \
        --workers 8

**Compatibility mode** (``--release-dir``), kept until the R generator retires
with acceptance criterion 1 (#288): the R path emits a working bundle first, so
acquisition reads its ``analyses.tsv`` and fills in what it acquired, exactly as
it did before this script became the Acquisition Manifest writer:

    python3 -m manifest_generation.providers.finngen.acquire --release-dir=<release dir>

It fetches each row's ``source_url`` into its ``source_file`` (resumable, the
same downloader), pins the bytes against the row's ``checksum`` when the row
carries one, writes ``<release-dir>/analyses.tsv`` back with ``checksum`` and
``size_bytes`` filled, ``<release-dir>/sidecars/downloads.tsv`` with the
download evidence, and merges ``checks.yaml.files`` into the bundle's
``validation.yaml``. Beside them it writes the shared Acquisition Manifest as
``<release-dir>/acquisition-manifest.tsv``, so the R path's acquisition is
recorded in the same format as every other source's.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import write_connector
from manifest_generation.release_yaml import merge_validation_yaml
from manifest_generation.source_inventory import (
    InventoryError,
    atomic_write,
    counts,
    sha256_file,
)

#: The provider-manifest columns acquisition reads; the rest are intent.
REQUIRED_MANIFEST_COLUMNS: tuple[str, ...] = ("phenocode", "path_https")

#: The Acquisition Manifest columns this writer emits, in canonical order.
OUTPUT_COLUMNS: tuple[str, ...] = ("source_analysis_id", "status", "location", "bytes", "sha256",
                                   "source_url", "error")

#: The file every endpoint's bytes are staged in, named from its phenocode.
FILE_NAME = "finngen_R13_{phenocode}.gz"

#: ``sidecars/downloads.tsv`` in compatibility mode: the columns the R path's
#: receipts read, unchanged since before this script wrote Acquisition Manifests.
DOWNLOADS_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "status",
    "source_url",
    "source_file",
    "source_etag",
    "size_bytes",
    "checksum_algorithm",
    "checksum",
    "error",
    "notes",
)

#: Where compatibility mode writes the shared Acquisition Manifest.
RELEASE_DIR_MANIFEST: str = "acquisition-manifest.tsv"

#: Compatibility mode's transfer statuses, as the R receipts spell them, and the
#: Acquisition Manifest readiness status each one records.
DOWNLOAD_STATUS: Mapping[str, str] = {
    "cached": "already_present",
    "downloaded": "ok",
    "resumed": "ok",
    "failed": "data_failed",
}


class AcquisitionError(InventoryError):
    """The provider manifest cannot be acquired as the run was asked."""


class ChecksumMismatch(OSError):
    """Bytes do not match the digest they are pinned to."""


def source_file_name(phenocode: str) -> str:
    return FILE_NAME.format(phenocode=phenocode)


def read_endpoint_manifest(path: Path) -> list[dict[str, str]]:
    """The provider manifest's rows, refusing a missing column or a duplicate phenocode."""
    path = Path(path)
    if not path.is_file():
        raise AcquisitionError(f"FinnGen endpoint manifest not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        missing = [name for name in REQUIRED_MANIFEST_COLUMNS if name not in (reader.fieldnames or [])]
        if missing:
            raise AcquisitionError(
                f"FinnGen endpoint manifest {path} is missing column(s): {', '.join(missing)}"
            )
        rows = [{name: (row.get(name) or "").strip() for name in REQUIRED_MANIFEST_COLUMNS}
                for row in reader]
    seen: set[str] = set()
    repeated: list[str] = []
    for row in rows:
        phenocode = row["phenocode"]
        if not phenocode:
            raise AcquisitionError(f"FinnGen endpoint manifest {path} has a row with no phenocode")
        if phenocode in seen:
            repeated.append(phenocode)
        seen.add(phenocode)
    if repeated:
        raise AcquisitionError(
            f"FinnGen endpoint manifest {path} lists phenocode(s) twice: {', '.join(sorted(repeated))}"
        )
    if not rows:
        raise AcquisitionError(f"FinnGen endpoint manifest {path} has no endpoints")
    return rows


def _total_size(response: Any, start: int) -> int | None:
    headers = response.headers
    content_range = headers.get("Content-Range", "")
    if "/" in content_range:
        try:
            return int(content_range.rsplit("/", 1)[1])
        except ValueError:
            return None
    try:
        return start + int(headers["Content-Length"])
    except (KeyError, TypeError, ValueError):
        return None


def fetch(
    url: str, target: Path, *, timeout: float, expected_sha256: str = ""
) -> tuple[str, str]:
    """Download ``url`` into ``target`` resumably, then promote it.

    Returns the transfer kind and the response's ETag. When ``expected_sha256``
    is given, the bytes are hashed before promotion and a mismatch raises
    :class:`ChecksumMismatch`, leaving the staged ``.part`` file in place and the
    target untouched: a pinned digest is an expectation, never something the
    downloaded bytes may overwrite.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    start = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(url)
    if start:
        request.add_header("Range", f"bytes={start}-")
    # The URL comes from the checksum-pinned provider manifest.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        status = getattr(response, "status", response.getcode())
        resumed = start > 0 and status == 206
        if start and not resumed:
            start = 0
        total = _total_size(response, start)
        etag = response.headers.get("ETag", "")
        with part.open("ab" if resumed else "wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
    if total is not None and part.stat().st_size != total:
        raise OSError(
            f"incomplete download for {url}: got {part.stat().st_size} bytes, expected {total}"
        )
    if expected_sha256:
        actual = sha256_file(part)
        if actual != expected_sha256:
            raise ChecksumMismatch(
                f"downloaded artifact {url} has sha256 {actual}, expected {expected_sha256}"
            )
    os.replace(part, target)
    return ("resumed" if resumed else "downloaded"), etag


def acquire_one(
    row: Mapping[str, str],
    source_dir: Path,
    *,
    timeout: float,
    recorded: Mapping[str, str] = {},
) -> dict[str, str]:
    """One endpoint's Acquisition Manifest row: its file, or the reason there is none.

    ``recorded`` is what the manifest this run replaces already says each
    endpoint's bytes are. A file that no longer matches is refused by name
    rather than being re-hashed into a fresh row: the digest is identity
    evidence, so the record keeps no ``location``, ``bytes`` or ``sha256``.
    """
    phenocode = row["phenocode"]
    target = source_dir / source_file_name(phenocode)
    record = {
        "source_analysis_id": phenocode,
        "source_url": row["path_https"],
        "location": "",
        "bytes": "",
        "sha256": "",
        "error": "",
    }
    expected = recorded.get(phenocode, "")
    try:
        if target.is_file() and expected:
            actual = sha256_file(target)
            if actual != expected:
                record["status"] = "data_failed"
                record["error"] = (
                    f"existing file {target} no longer matches the sha256 the manifest this run "
                    f"replaces recorded for {phenocode}: recorded {expected}, found {actual}. "
                    "Identity no longer matches, so the file is refused rather than re-hashed; "
                    "remove it to re-acquire, or restore the recorded bytes"
                )
                return record
        if target.is_file():
            record["status"] = "already_present"
        else:
            fetch(row["path_https"], target, timeout=timeout)
            record["status"] = "ok"
        record["location"] = str(target)
        record["bytes"] = str(target.stat().st_size)
        record["sha256"] = sha256_file(target)
    except urllib.error.HTTPError as exc:
        record["status"] = (
            "data_absent_upstream" if exc.code == 404 else "data_failed"
        )
        record["error"] = f"HTTP {exc.code} {exc.reason} for {row['path_https']}"
    except Exception as exc:  # noqa: BLE001 - every failure is per-endpoint evidence
        record["status"] = "data_failed"
        record["error"] = f"{exc.__class__.__name__}: {exc}"
    return record


def write_pass_lineage(
    out: Path,
    provider_manifest: Path,
    source_dir: Path,
    readiness: Mapping[str, int],
    *,
    rows: int,
    selected: int,
    workers: int,
    timeout: float,
) -> Path:
    """Record the provider manifest, the written manifest and the run beside it."""
    sidecar = pass_lineage_path(out)
    document = {
        "acquisition_manifest": str(out),
        "sha256": sha256_file(out),
        "rows": rows,
        "readiness_counts": dict(readiness),
        "written_by": __spec__.name,
        "provider_manifest": {
            "path": str(provider_manifest),
            "sha256": sha256_file(provider_manifest),
            "bytes": provider_manifest.stat().st_size,
            "rows": len(read_endpoint_manifest(provider_manifest)),
        },
        "source_dir": str(source_dir),
        "runtime": {"selected": selected, "workers": workers, "timeout_seconds": timeout},
    }
    atomic_write(
        sidecar, yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)
    )
    return sidecar


def recorded_checksums(out: Path) -> dict[str, str]:
    """The digests the Acquisition Manifest at ``out`` already records, per Analysis.

    A run re-acquiring into the same manifest is held to them; a manifest that
    does not exist yet (a new pass) pins nothing, and the provider publishes no
    digests to pin the first run against.
    """
    out = Path(out)
    if not out.is_file():
        return {}
    manifest = load_acquisition_manifest(out)
    return {
        entry.source_analysis_id: entry.sha256
        for entry in manifest.entries
        if entry.sha256
    }


def acquire_endpoints(
    provider_manifest: Path,
    source_dir: Path,
    out: Path,
    *,
    workers: int = 4,
    only_analysis_ids: Sequence[str] = (),
    timeout: float = 120.0,
) -> tuple[list[dict[str, str]], Path]:
    """Acquire every selected endpoint, write the Acquisition Manifest and its lineage."""
    if workers < 1:
        raise AcquisitionError("--workers must be at least 1")
    provider_manifest, source_dir, out = (
        Path(provider_manifest), Path(source_dir), Path(out)
    )
    rows = read_endpoint_manifest(provider_manifest)
    wanted = [value for value in only_analysis_ids if value]
    if wanted:
        known = {row["phenocode"] for row in rows}
        unknown = sorted(set(wanted) - known)
        if unknown:
            raise AcquisitionError(
                f"--only-analysis-id names phenocode(s) the provider manifest does not list: "
                f"{', '.join(unknown)}"
            )
        wanted_set = set(wanted)
        rows = [row for row in rows if row["phenocode"] in wanted_set]
    source_dir.mkdir(parents=True, exist_ok=True)
    recorded = recorded_checksums(out)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        acquired = list(
            executor.map(
                lambda row: acquire_one(row, source_dir, timeout=timeout, recorded=recorded),
                rows,
            )
        )
    write_connector(out, ACQUISITION_MANIFEST, OUTPUT_COLUMNS, acquired)
    try:
        manifest = load_acquisition_manifest(out)
    except InventoryError:
        out.unlink()
        raise
    readiness = counts(record["status"] for record in acquired)
    sidecar = write_pass_lineage(
        out, provider_manifest, source_dir, readiness,
        rows=len(acquired), selected=len(acquired), workers=workers, timeout=timeout,
    )
    return acquired, sidecar



# ---------------------------------------------------------------------------
# Compatibility mode: the R path's working bundle (until acceptance criterion 1)
# ---------------------------------------------------------------------------


def acquire_file(
    url: str,
    target: Path,
    *,
    timeout: float,
    expected_sha256: str = "",
    previous_etag: str = "",
) -> tuple[str, str, str]:
    """Fetch or reuse one file; return its transfer status, ETag and sha256.

    A complete ``target`` is checksummed, never refetched, and held to
    ``expected_sha256`` when one is pinned.
    """
    if target.is_file():
        checksum = sha256_file(target)
        if expected_sha256 and checksum != expected_sha256:
            raise ChecksumMismatch(
                f"existing artifact {target} has sha256 {checksum}, "
                f"expected {expected_sha256}"
            )
        return "cached", previous_etag, checksum
    status, etag = fetch(url, target, timeout=timeout, expected_sha256=expected_sha256)
    return status, etag, sha256_file(target)


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader.fieldnames or ()), list(reader)


def _write_rows(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _legacy_report(
    row: Mapping[str, str], previous: Mapping[str, Mapping[str, str]], *, timeout: float
) -> dict[str, str]:
    """One working-bundle row's download evidence: what happened, or why it failed."""
    analysis_id = row["analysis_id"]
    target = Path(row["source_file"])
    expected = row.get("checksum", "") or ""
    report: dict[str, str] = {
        "analysis_id": analysis_id,
        "status": "",
        "source_url": row.get("source_url", ""),
        "source_file": str(target),
        "source_etag": "",
        "size_bytes": "",
        "checksum_algorithm": "sha256",
        "checksum": "",
        "error": "",
        "notes": "",
    }
    try:
        status, etag, checksum = acquire_file(
            row.get("source_url", ""),
            target,
            timeout=timeout,
            expected_sha256=expected,
            previous_etag=previous.get(analysis_id, {}).get("source_etag", ""),
        )
    except Exception as exc:  # noqa: BLE001 - per-artifact failure evidence
        report.update(status="failed", error=str(exc), notes="acquisition failed")
        return report
    size = target.stat().st_size
    report.update(
        status=status,
        source_etag=etag,
        size_bytes=str(size),
        checksum=checksum,
        notes="checksum verified",
    )
    return report


def acquire_release_dir(
    release_dir: Path,
    *,
    workers: int = 4,
    only_analysis_ids: Sequence[str] = (),
    timeout: float = 120.0,
) -> tuple[list[dict[str, str]], Path]:
    """Acquire a R-emitted working bundle's sources; return its download evidence.

    Fills the bundle's ``analyses.tsv`` checksums, writes its
    ``sidecars/downloads.tsv`` and its ``validation.yaml`` check, and writes the
    shared Acquisition Manifest beside them (``acquisition-manifest.tsv``), so
    the R path's acquisition is recorded in the same format as any other
    source's. Refuses to proceed on any failure, after writing the evidence.
    """
    if workers < 1:
        raise AcquisitionError("--workers must be at least 1")
    release_dir = Path(release_dir)
    analyses_path = release_dir / "analyses.tsv"
    if not analyses_path.is_file():
        raise AcquisitionError(
            f"{analyses_path} not found: run the generator's emit mode first"
        )
    columns, rows = _read_rows(analyses_path)
    if not rows:
        raise AcquisitionError(f"{analyses_path} contains no rows")
    for required in ("analysis_id", "source_url", "source_file"):
        if required not in columns:
            raise AcquisitionError(f"{analyses_path} is missing column {required!r}")

    downloads_path = release_dir / "sidecars" / "downloads.tsv"
    previous = (
        {row["analysis_id"]: row for row in _read_rows(downloads_path)[1]}
        if downloads_path.is_file()
        else {}
    )
    wanted = {value for value in only_analysis_ids if value}
    selected = [row for row in rows if not wanted or row["analysis_id"] in wanted]
    if not selected:
        raise AcquisitionError(
            "--only-analysis-id selected no Analysis in " + str(analyses_path)
        )
    with ThreadPoolExecutor(max_workers=workers) as executor:
        reports = list(
            executor.map(
                lambda row: _legacy_report(row, previous, timeout=timeout), selected
            )
        )
    by_id = {report["analysis_id"]: report for report in reports}
    for row in rows:
        report = by_id.get(row["analysis_id"])
        if report is None or report["status"] == "failed":
            continue
        row["checksum_algorithm"] = "sha256"
        row["checksum"] = report["checksum"]
        row["size_bytes"] = report["size_bytes"]
    _write_rows(analyses_path, columns, rows)

    evidence = [
        by_id.get(row["analysis_id"]) or previous.get(row["analysis_id"], {})
        for row in rows
    ]
    evidence = [row for row in evidence if row]
    _write_rows(downloads_path, DOWNLOADS_COLUMNS, evidence)

    failed_ids = {
        report["analysis_id"] for report in evidence if report.get("status") == "failed"
    }
    failures = [
        f"{report['analysis_id']}: {report['error'] or 'acquisition failed'}"
        for report in evidence
        if report.get("status") == "failed"
    ]
    missing = [
        row["analysis_id"] for row in rows if row["analysis_id"] not in by_id
        and row["analysis_id"] not in previous
    ]
    failed_ids.update(missing)
    failures.extend(f"{analysis_id}: missing download evidence" for analysis_id in missing)

    manifest_path = write_release_dir_manifest(release_dir, rows, evidence, failed_ids)
    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name=__spec__.name,
        updated_checks={"files": "failed" if failures else "passed"},
        updated_reports={"downloads": "sidecars/downloads.tsv"},
        new_warnings=failures,
    )
    if failures:
        raise AcquisitionError("FinnGen acquisition failed:\n" + "\n".join(failures))
    return evidence, manifest_path


def write_release_dir_manifest(
    release_dir: Path,
    rows: Sequence[Mapping[str, str]],
    evidence: Sequence[Mapping[str, str]],
    failed_ids: Sequence[str],
) -> Path:
    """Record compatibility mode's acquisition as the shared Acquisition Manifest."""
    by_id = {report["analysis_id"]: report for report in evidence}
    records: list[dict[str, str]] = []
    for row in rows:
        report = by_id.get(row["analysis_id"], {})
        failed = report.get("status") == "failed" or row["analysis_id"] in set(failed_ids)
        ready = report.get("status") in {"cached", "downloaded", "resumed"} and not failed
        records.append({
            "source_analysis_id": row.get("source_analysis_id") or row["analysis_id"],
            "status": DOWNLOAD_STATUS.get(report.get("status", ""), "data_failed"),
            "location": row["source_file"] if ready else "",
            "bytes": report.get("size_bytes", "") if ready else "",
            "sha256": report.get("checksum", "") if ready else "",
            "source_url": row.get("source_url", ""),
            "error": report.get("error", ""),
        })
    out = Path(release_dir) / RELEASE_DIR_MANIFEST
    write_connector(out, ACQUISITION_MANIFEST, OUTPUT_COLUMNS, records)
    load_acquisition_manifest(out)
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--manifest", type=Path,
        help="FinnGen's finngen_R13_manifest.tsv (the provider's endpoint list)",
    )
    parser.add_argument(
        "--source-dir", type=Path,
        help="the directory every endpoint's file is fetched into",
    )
    parser.add_argument(
        "--out", type=Path, help="the Acquisition Manifest to write",
    )
    parser.add_argument(
        "--release-dir", type=Path,
        help="compatibility mode: an R-emitted working bundle whose analyses.tsv "
             "this fills in, kept until the R generator retires (#288)",
    )
    parser.add_argument(
        "--only-analysis-id", default="",
        help="comma-separated phenocodes to acquire; default: every endpoint",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request seconds")
    return parser.parse_args(argv)


def _parse_or_refuse(args: argparse.Namespace) -> None:
    """Exactly one interface: the Acquisition Manifest mode, or ``--release-dir``."""
    named = [name for name in ("manifest", "source_dir", "out") if getattr(args, name)]
    if args.release_dir:
        if named:
            raise AcquisitionError(
                "--release-dir replaces --manifest, --source-dir and --out; it cannot be "
                f"combined with {', '.join('--' + name.replace('_', '-') for name in named)}"
            )
        return
    missing = [
        "--" + name.replace("_", "-") for name in ("manifest", "source_dir", "out")
        if not getattr(args, name)
    ]
    if missing:
        raise AcquisitionError(
            "the Acquisition Manifest mode needs " + ", ".join(missing)
            + " (or --release-dir for the R path's working bundle)"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    requested = [value.strip() for value in args.only_analysis_id.split(",") if value.strip()]
    try:
        _parse_or_refuse(args)
        if args.release_dir:
            evidence, manifest_path = acquire_release_dir(
                args.release_dir,
                workers=args.workers,
                only_analysis_ids=requested,
                timeout=args.timeout,
            )
            print(
                f"Acquired {len(evidence)} FinnGen artifacts into {args.release_dir} "
                f"({sum(report['status'] == 'cached' for report in evidence)} cached); "
                f"Acquisition Manifest {manifest_path}"
            )
            return 0
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        acquired, sidecar = acquire_endpoints(
            args.manifest, args.source_dir, args.out,
            workers=args.workers, only_analysis_ids=requested, timeout=args.timeout,
        )
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    failures = [record for record in acquired if record["status"] not in {"ok", "already_present"}]
    present = len(acquired) - len(failures)
    print(
        f"Acquisition Manifest {args.out} written from {args.manifest}: "
        f"{present} ready, {len(failures)} without a usable file; provenance {sidecar}"
    )
    if failures:
        print(
            "FinnGen acquisition failed for:\n  - "
            + "\n  - ".join(
                f"{record['source_analysis_id']}: {record['status']} {record['error']}"
                for record in failures
            ),
            file=sys.stderr,
        )
        # The manifest is the evidence, including the absences; the exit code is
        # the retry signal a wrapper needs.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
