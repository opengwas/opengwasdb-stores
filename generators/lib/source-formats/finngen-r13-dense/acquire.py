#!/usr/bin/env python3
"""Acquire FinnGen R13 endpoint artifacts with resumable, idempotent downloads."""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from generators.lib.release_yaml import merge_validation_yaml, read_tsv  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--only-analysis-id", default="")
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_tsv(
    path: Path, rows: Sequence[Mapping[str, object]], fieldnames: list[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _total_size(response: object, start: int) -> int | None:
    headers = response.headers  # type: ignore[attr-defined]
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


def acquire(url: str, target: Path, expected_checksum: str = "") -> tuple[str, str, str]:
    """Return status, ETag, and SHA-256 after validating then promoting `.part`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    start = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(url)
    if start:
        request.add_header("Range", f"bytes={start}-")

    # The URL is supplied by the checksum-pinned provider manifest.
    with urllib.request.urlopen(request) as response:  # noqa: S310
        response_status = getattr(response, "status", response.getcode())
        resumed = start > 0 and response_status == 206
        if start and not resumed:
            start = 0
        mode = "ab" if resumed else "wb"
        total_size = _total_size(response, start)
        etag = response.headers.get("ETag", "")
        with part.open(mode) as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)

    if total_size is not None and part.stat().st_size != total_size:
        raise OSError(
            f"incomplete download for {url}: got {part.stat().st_size} bytes, expected {total_size}"
        )
    checksum = sha256_file(part)
    if expected_checksum and checksum != expected_checksum:
        raise OSError(
            f"downloaded artifact checksum {checksum} does not match manifest "
            f"{expected_checksum}"
        )
    os.replace(part, target)
    return ("resumed" if resumed else "downloaded"), etag, checksum


def acquire_one(
    row: dict[str, str], previous: dict[str, dict[str, str]]
) -> tuple[dict[str, object], str]:
    analysis_id = row["analysis_id"]
    target = Path(row["source_file"])
    expected_checksum = row.get("checksum", "")
    try:
        if target.exists():
            checksum = sha256_file(target)
            if expected_checksum and checksum != expected_checksum:
                raise OSError(
                    f"existing artifact checksum {checksum} does not match manifest "
                    f"{expected_checksum}"
                )
            status = "cached"
            etag = previous.get(analysis_id, {}).get("source_etag", "")
        else:
            status, etag, checksum = acquire(
                row["source_url"], target, expected_checksum
            )
        size = target.stat().st_size
        row["checksum_algorithm"] = "sha256"
        row["checksum"] = checksum
        row["size_bytes"] = str(size)
        report: dict[str, object] = {
            "analysis_id": analysis_id,
            "status": status,
            "source_url": row["source_url"],
            "source_file": str(target),
            "source_etag": etag,
            "size_bytes": size,
            "checksum_algorithm": "sha256",
            "checksum": checksum,
            "error": "",
            "notes": "checksum verified",
        }
        print(f"{analysis_id}: {status} ({size} bytes)", flush=True)
        return report, ""
    except Exception as exc:  # noqa: BLE001 - preserve per-artifact failure evidence
        error = f"{analysis_id}: {exc}"
        return {
            "analysis_id": analysis_id,
            "status": "failed",
            "source_url": row.get("source_url", ""),
            "source_file": str(target),
            "source_etag": "",
            "size_bytes": "",
            "checksum_algorithm": "sha256",
            "checksum": "",
            "error": str(exc),
            "notes": "acquisition failed",
        }, error


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    release_dir = Path(args.release_dir).resolve()
    analyses_path = release_dir / "analyses.tsv"
    rows = read_tsv(analyses_path)
    if not rows:
        raise SystemExit("analyses.tsv contains no rows")
    requested = {value for value in args.only_analysis_id.split(",") if value}
    selected = [row for row in rows if not requested or row["analysis_id"] in requested]
    if not selected:
        raise SystemExit("No analyses selected")

    previous_downloads_path = release_dir / "sidecars" / "downloads.tsv"
    previous = {
        row["analysis_id"]: row for row in read_tsv(previous_downloads_path)
    } if previous_downloads_path.exists() else {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        outcomes = list(executor.map(lambda row: acquire_one(row, previous), selected))
    selected_reports = {
        str(report["analysis_id"]): report for report, _error in outcomes
    }
    errors = [error for _report, error in outcomes if error]
    reports = [
        selected_reports[row["analysis_id"]]
        if row["analysis_id"] in selected_reports
        else previous[row["analysis_id"]]
        for row in rows
        if row["analysis_id"] in selected_reports or row["analysis_id"] in previous
    ]
    reports_by_id = {str(report["analysis_id"]): report for report in reports}
    missing_evidence = [
        row["analysis_id"] for row in rows if row["analysis_id"] not in reports_by_id
    ]
    evidence_errors = [
        *errors,
        *(f"{analysis_id}: missing download evidence" for analysis_id in missing_evidence),
        *(
            f"{report['analysis_id']}: {report.get('error') or 'acquisition failed'}"
            for report in reports
            if report.get("status") == "failed"
        ),
    ]
    fieldnames = list(rows[0])
    write_tsv(analyses_path, rows, fieldnames)
    report_path = release_dir / "sidecars" / "downloads.tsv"
    report_fields = list(reports[0])
    write_tsv(report_path, reports, report_fields)
    merge_validation_yaml(
        release_dir / "validation.yaml",
        validator_name="generators/lib/source-formats/finngen-r13-dense/acquire.py",
        updated_checks={"files": "failed" if evidence_errors else "passed"},
        updated_reports={"downloads": "sidecars/downloads.tsv"},
        new_warnings=evidence_errors,
    )
    if evidence_errors:
        raise SystemExit("FinnGen acquisition failed:\n" + "\n".join(evidence_errors))
    print(f"Acquired {len(selected)} FinnGen artifacts ({sum(r['status'] == 'cached' for r in reports)} cached)")


if __name__ == "__main__":
    main()
