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

Usage:
    python3 -m manifest_generation.providers.finngen.acquire \\
        --manifest /data/opengwasdb/raw/finngen-r13/releases/r13-full/manifest/finngen_R13_manifest.tsv \\
        --source-dir /data/opengwasdb/raw/finngen-r13/releases/r13-full/source \\
        --out /data/opengwasdb/raw/finngen-r13/connectors/<date>/finngen-r13-full.acquisition-manifest.tsv \\
        --workers 8
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


class AcquisitionError(InventoryError):
    """The provider manifest cannot be acquired as the run was asked."""


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


def fetch(url: str, target: Path, *, timeout: float) -> str:
    """Download ``url`` into ``target`` resumably, then promote it; return the transfer kind."""
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
        with part.open("ab" if resumed else "wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
    if total is not None and part.stat().st_size != total:
        raise OSError(
            f"incomplete download for {url}: got {part.stat().st_size} bytes, expected {total}"
        )
    os.replace(part, target)
    return "resumed" if resumed else "downloaded"


def acquire_one(row: Mapping[str, str], source_dir: Path, *, timeout: float) -> dict[str, str]:
    """One endpoint's Acquisition Manifest row: its file, or the reason there is none."""
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
    try:
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
    with ThreadPoolExecutor(max_workers=workers) as executor:
        acquired = list(
            executor.map(lambda row: acquire_one(row, source_dir, timeout=timeout), rows)
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--manifest", required=True, type=Path,
        help="FinnGen's finngen_R13_manifest.tsv (the provider's endpoint list)",
    )
    parser.add_argument(
        "--source-dir", required=True, type=Path,
        help="the directory every endpoint's file is fetched into",
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="the Acquisition Manifest to write",
    )
    parser.add_argument(
        "--only-analysis-id", default="",
        help="comma-separated phenocodes to acquire; default: every endpoint",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    requested = [value.strip() for value in args.only_analysis_id.split(",") if value.strip()]
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
