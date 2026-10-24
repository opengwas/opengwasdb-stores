#!/usr/bin/env python3
"""Write the ukb-b Acquisition Manifest from the recorded identity and integrity receipts (#289).

ukb-b's files were copied into the mirror, not downloaded by an acquisition
run, so there is no transfer to report. What the restored inventory recorded
once (#219) is the mirror's identity, and this writer turns that record -- not a
fresh 425 GB read -- into the canonical Acquisition Manifest
(``manifest_generation.acquisition_manifest``) the shared freeze reads:

- ``--checksums``: the resumable checksum receipt, one entry per Analysis with
  its ``data_file``, ``data_bytes``, ``mtime_ns`` and ``sha256``
  (``/data/opengwasdb/work/epic232/219/ukb-b-restored-2026-10-11.checksums.json``);
- ``--integrity``: the deep compressed-stream receipt, one status per Analysis
  (``...integrity.json``: ``ok``, ``missing_file``, ``missing_index``,
  ``bad_index`` or ``integrity_failed``, from ``bgzip -t`` and ``tabix -l``);
- ``--verification``: the deep identity re-verification report (required: an
  absent, shallow, empty, partial or self-contradictory one is refused)
  (``...verify-deep.json``: ``missing``, ``size_mismatch``,
  ``checksum_mismatch``, ``index_mismatch``, ``mtime_changed``).

Per declared Analysis the writer records its VCF (``location``, ``bytes``,
``sha256``) and its provider JSON sidecar (``sidecar_location``,
``sidecar_bytes``), and derives the readiness ``status`` from the recorded
evidence *and* the cheap checks a writer can make without reading a byte: the
file is still there, its size is still the recorded one, and its ``.tbi`` index
is still there. The order is that one -- presence, size, checksum, index
presence, index identity, stream integrity -- so the most fundamental failure
is the one reported:

| Status | Meaning |
|---|---|
| ``ok`` | present, the recorded size and checksum, its index present and openable, and the stream decompresses. |
| ``missing_file`` | the VCF (or, in the integrity receipt, its index) is gone. |
| ``size_mismatch`` | the file's size is not the recorded one: a truncation. |
| ``checksum_mismatch`` | the deep verification's sha256 is not the recorded one. |
| ``missing_index`` | the ``.tbi`` beside the VCF is gone. |
| ``index_mismatch`` | the index's sha256 or size is not the recorded one. |
| ``bad_index`` | the index does not open or lists no contig. |
| ``integrity_failed`` | the compressed stream does not decompress to EOF. |

Those are ``source_inventory.SOURCE_VERIFICATION_STATUSES`` (#289), none of
them ready: a non-ready row is an *explained* absence, kept in the frozen
inventory as the fact it is and never a release member. A non-ready row keeps
the recorded ``location``, ``bytes`` and ``sha256`` -- the identity the run
holds the Analysis to -- so the refusal does not erase its own pin, and the
``error`` says which check failed and what it saw.

The verification report is evidence or it is refused: it must say it was deep,
how many files it verified and what it found, and its `ok` must agree with the
failures it lists. Every failure it names -- `missing` included -- becomes that
row's non-ready status, so a file the deep verification found absent is a
`missing_file` row with the reason.

The writer refuses a declared Analysis the recorded evidence does not cover:
silently marking one ready, or inventing an identity for it, would be guessing
about a frozen input. A sidecar ``<out-stem>.meta.yaml`` records each receipt's
path, sha256 and row count and the manifest's readiness counts, so the manifest
traces back to the evidence it was written from (the shared freeze checksums it
into the inventory's provenance as the pass lineage).

Usage:
    python3 -m manifest_generation.providers.ukb_b.write_acquisition_manifest \\
        --declared-selection resources/inventories/ukb-b-restored-2026-10-11.declared-selection.tsv \\
        --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b \\
        --checksums /data/opengwasdb/work/epic232/219/ukb-b-restored-2026-10-11.checksums.json \\
        --integrity /data/opengwasdb/work/epic232/219/ukb-b-restored-2026-10-11.integrity.json \\
        --verification /data/opengwasdb/work/epic232/219/verify-deep.json \\
        --out <acquisition-manifest.tsv>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from manifest_generation.acquisition_manifest import (
    ACQUISITION_MANIFEST,
    load_acquisition_manifest,
    pass_lineage_path,
)
from manifest_generation.connector_tsv import listed, raise_problems, write_connector
from manifest_generation.providers.ukb_b.source_metadata_adapter import (
    declared_analysis_id,
    read_declared_selection,
)
from manifest_generation.source_inventory import InventoryError, atomic_write, sha256_file

#: The canonical columns the writer emits.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "source_analysis_id",
    "status",
    "location",
    "bytes",
    "sha256",
    "sidecar_location",
    "sidecar_bytes",
    "error",
)

#: The data file's suffix and its Tabix index's, in the mirror.
SOURCE_SUFFIX: str = ".vcf.gz"
INDEX_SUFFIX: str = ".vcf.gz.tbi"
SIDECAR_SUFFIX: str = ".json"

#: The deep verification report's per-Analysis lists, and the status each one
#: makes the row. The report spells an entry ``<analysis_id> (<detail>)``.
VERIFICATION_STATUSES: tuple[tuple[str, str], ...] = (
    ("missing", "missing_file"),
    ("size_mismatch", "size_mismatch"),
    ("checksum_mismatch", "checksum_mismatch"),
    ("index_mismatch", "index_mismatch"),
)

#: The summary a deep verification report carries: what it verified and how.
VERIFICATION_SUMMARY_KEYS: tuple[str, ...] = ("ok", "deep", "rows")

#: The deep integrity receipt's statuses, and the readiness status each one is.
INTEGRITY_STATUSES: Mapping[str, str] = {
    "ok": "ok",
    "missing_file": "missing_file",
    "missing_index": "missing_index",
    "bad_index": "bad_index",
    "integrity_failed": "integrity_failed",
}

_ENTRY_DETAIL = re.compile(r"\A(\S+)(?:\s+\((.*)\))?\Z")


class WriterError(InventoryError):
    """The recorded evidence cannot be turned into a usable Acquisition Manifest."""


class RecordedIdentity:
    """One Analysis's recorded mirror identity, from the checksum receipt."""

    def __init__(self, analysis_id: str, entry: Mapping[str, Any]) -> None:
        self.analysis_id = analysis_id
        self.location = str(entry.get("data_file") or "")
        self.bytes = entry.get("data_bytes")
        self.mtime_ns = entry.get("mtime_ns")
        self.sha256 = str(entry.get("sha256") or "")


class Evidence:
    """The recorded receipts one manifest is written from, read once."""

    def __init__(
        self,
        checksums: Path,
        integrity: Path,
        verification: Path,
    ) -> None:
        self.checksums_path = Path(checksums)
        self.integrity_path = Path(integrity)
        # Required, not optional: without a deep verification the writer cannot
        # know a checksum is intact, and an absent report must never read as
        # "nothing failed" (#289 review).
        self.verification_path = Path(verification)
        self.identities: dict[str, RecordedIdentity] = {
            analysis_id: RecordedIdentity(analysis_id, entry)
            for analysis_id, entry in _entries(self.checksums_path, "checksum receipt").items()
        }
        self.integrity: dict[str, dict[str, Any]] = {
            analysis_id: dict(entry)
            for analysis_id, entry in _entries(self.integrity_path, "integrity receipt").items()
        }
        self.verification = _verification_report(self.verification_path)

    def digest(self) -> dict[str, Any]:
        """Each receipt's path, sha256 and row count, for the pass lineage."""
        recorded = {
            "checksums": (self.checksums_path, len(self.identities)),
            "integrity": (self.integrity_path, len(self.integrity)),
        }
        recorded["verification"] = (self.verification_path, self.verification.rows)
        digest = {
            name: {"path": str(path), "sha256": sha256_file(path), "rows": rows}
            for name, (path, rows) in recorded.items()
        }
        # What the verification refused, beside how many files it verified.
        digest["verification"]["failures"] = len(self.verification.statuses)
        return digest


def _entries(path: Path, what: str) -> Mapping[str, Mapping[str, Any]]:
    """The ``entries`` mapping of a recorded receipt, refusing anything else."""
    path = Path(path)
    if not path.is_file():
        raise WriterError(f"ukb-b {what} not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WriterError(f"ukb-b {what} {path} is not readable JSON: {exc}") from exc
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise WriterError(f"ukb-b {what} {path} records no 'entries' mapping")
    for analysis_id, entry in entries.items():
        if not isinstance(entry, dict):
            raise WriterError(f"ukb-b {what} {path}: entry {analysis_id!r} is not an object")
    return entries


class VerificationReport:
    """A deep verification report: how many files it verified, and what it refused.

    ``statuses`` is ``analysis_id -> readiness status`` for every Analysis the
    report refused, so a missing, truncated, re-checksummed or re-indexed file
    becomes that row's non-ready status instead of disappearing.
    """

    def __init__(self, path: Path, rows: int, statuses: Mapping[str, str]) -> None:
        self.path = path
        self.rows = rows
        self.statuses = dict(statuses)


def _verification_report(path: Path) -> VerificationReport:
    """Read the deep verification report, refusing an unusable one by name.

    A report that verified nothing is not evidence: `{}`, a shallow report, a
    report of zero rows, one that does not say what it verified, one whose `ok`
    contradicts the failures it lists, and one that names no failures while
    saying it found some are each refused rather than read as an empty success
    list.
    """
    path = Path(path)
    if not path.is_file():
        raise WriterError(f"ukb-b deep verification report not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WriterError(f"ukb-b deep verification report {path} is not readable JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise WriterError(f"ukb-b deep verification report {path} is not a JSON object")
    problems: list[str] = []
    for key in VERIFICATION_SUMMARY_KEYS:
        if key not in document:
            problems.append(f"{key!r} is missing")
    if "ok" in document and not isinstance(document["ok"], bool):
        problems.append(f"'ok' {document['ok']!r} is not true or false")
    if "deep" in document and document["deep"] is not True:
        problems.append(
            f"'deep' is not true ({document['deep']!r}): a shallow report compares sizes and "
            "mtimes, never a checksum"
        )
    if "rows" in document:
        rows = document["rows"]
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 1:
            problems.append(f"'rows' {rows!r} is not a whole number of verified Analyses")
    for key, _status in VERIFICATION_STATUSES:
        if key not in document:
            problems.append(f"{key!r} is missing")
        elif not isinstance(document[key], list):
            problems.append(f"{key!r} {document[key]!r} is not a list")
    if problems:
        raise_problems(WriterError, f"ukb-b deep verification report {path}", problems)

    statuses: dict[str, str] = {}
    for key, status in VERIFICATION_STATUSES:
        for value in document[key]:
            match = _ENTRY_DETAIL.match(str(value).strip())
            if match is None:
                raise WriterError(
                    f"ukb-b deep verification report {path}: {key!r} entry {value!r} names no Analysis"
                )
            statuses.setdefault(match.group(1), status)
    problems = []
    if document["ok"] is True and statuses:
        problems.append(
            "its 'ok' is true but it lists failures: "
            + listed(sorted(statuses))
        )
    if document["ok"] is False and not statuses:
        problems.append("its 'ok' is false but it names no Analysis and no failure")
    if problems:
        raise_problems(WriterError, f"ukb-b deep verification report {path}", problems)
    return VerificationReport(path=path, rows=document["rows"], statuses=statuses)


def write_from_evidence(
    declared_selection: Path,
    source_dir: Path,
    evidence: Evidence,
    out: Path,
) -> Path:
    """Write the Acquisition Manifest at ``out`` and its pass lineage; return the lineage path."""
    declared_selection, source_dir, out = Path(declared_selection), Path(source_dir), Path(out)
    declared = read_declared_selection(declared_selection)
    if not source_dir.is_dir():
        raise WriterError(f"ukb-b source directory not found: {source_dir}")
    rows, problems = build_rows(
        [declared_analysis_id(row) for row in declared], source_dir, evidence
    )
    if problems:
        raise_problems(WriterError, f"ukb-b recorded evidence for {source_dir}", problems)
    write_connector(out, ACQUISITION_MANIFEST, OUTPUT_COLUMNS, rows)
    try:
        manifest = load_acquisition_manifest(out)
    except InventoryError:
        out.unlink()
        raise
    lineage = pass_lineage_path(out)
    document = {
        "acquisition_manifest": str(out),
        "sha256": manifest.sha256,
        "rows": len(manifest.entries),
        "readiness_counts": manifest.readiness_counts,
        "written_by": "manifest_generation.providers.ukb_b.write_acquisition_manifest",
        "evidence": evidence.digest(),
        "readiness_rule": "presence, size, checksum, index presence, index identity, stream "
        "integrity; the first failing check is the status and the error says what it saw",
    }
    atomic_write(lineage, yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100))
    return lineage


def build_rows(
    analysis_ids: Iterable[str],
    source_dir: Path,
    evidence: Evidence,
) -> tuple[list[dict[str, str]], list[str]]:
    """One canonical manifest row per declared Analysis, and every problem the evidence has."""
    source_dir = Path(source_dir)
    analysis_ids = list(analysis_ids)
    report = evidence.verification
    problems: list[str] = []
    # Coverage: the report is evidence about *these* Analyses or it is not
    # evidence about them.
    if report.rows != len(analysis_ids):
        problems.append(
            f"the deep verification report's 'rows' is {report.rows}, but this Selection has "
            f"{len(analysis_ids)} Analysis/Analyses"
        )
    unknown = sorted(set(report.statuses) - set(analysis_ids))
    if unknown:
        problems.append(
            f"the deep verification report names Analysis/Analyses this Selection does not "
            f"contain (not selected): {listed(unknown)}"
        )
    if problems:
        raise_problems(WriterError, f"ukb-b deep verification report {report.path}", problems)
    rows: list[dict[str, str]] = []
    for analysis_id in analysis_ids:
        identity = evidence.identities.get(analysis_id)
        if identity is None:
            problems.append(
                f"{analysis_id}: the recorded checksum receipt has no entry for it, so its file's "
                "identity is not evidence"
            )
            continue
        location = Path(identity.location)
        if location.parent != source_dir:
            problems.append(
                f"{analysis_id}: the recorded file {identity.location!r} is not in {source_dir}"
            )
            continue
        if location.name != f"{analysis_id}{SOURCE_SUFFIX}":
            problems.append(
                f"{analysis_id}: the recorded file {location.name!r} is not "
                f"{analysis_id}{SOURCE_SUFFIX}"
            )
            continue
        sidecar = source_dir / f"{analysis_id}{SIDECAR_SUFFIX}"
        if not sidecar.is_file():
            problems.append(
                f"{analysis_id}: the provider JSON sidecar {sidecar} is missing, so the Analysis "
                "has no metadata to freeze"
            )
            continue
        status, error = _readiness(
            analysis_id, location, identity, evidence
        )
        rows.append(
            {
                "source_analysis_id": analysis_id,
                "status": status,
                "location": str(location),
                "bytes": _size(identity.bytes),
                "sha256": identity.sha256,
                "sidecar_location": str(sidecar),
                "sidecar_bytes": str(sidecar.stat().st_size),
                "error": error,
            }
        )
    return rows, problems


def _readiness(
    analysis_id: str,
    location: Path,
    identity: RecordedIdentity,
    evidence: Evidence,
) -> tuple[str, str]:
    """The Analysis's readiness status and, when it is not ready, what the check saw."""
    verified = evidence.verification.statuses.get(analysis_id)
    index = location.with_name(location.name + ".tbi")
    integrity = evidence.integrity.get(analysis_id)
    if verified == "missing_file" and location.is_file():
        # The verification found it gone; it is back now, which is a stale
        # snapshot rather than a verified file, and the row says so.
        return (
            "missing_file",
            f"missing_file: the deep verification found {location} absent, and it has since "
            "reappeared; re-verify before freezing",
        )
    if not location.is_file():
        return "missing_file", f"missing_file: {location} is not present"
    recorded_bytes = identity.bytes if isinstance(identity.bytes, int) else None
    size = location.stat().st_size
    if verified == "size_mismatch" or (recorded_bytes is not None and size != recorded_bytes):
        return (
            "size_mismatch",
            f"size_mismatch: {size} bytes, the recorded size is {identity.bytes}",
        )
    if verified == "checksum_mismatch":
        return (
            "checksum_mismatch",
            f"checksum_mismatch: the deep verification did not find the recorded sha256 "
            f"{identity.sha256}",
        )
    if not index.is_file():
        return "missing_index", f"missing_index: {index} is not present"
    if verified == "index_mismatch":
        return "index_mismatch", f"index_mismatch: {index} is not the recorded index"
    if integrity is None:
        return (
            "integrity_failed",
            "integrity_failed: the integrity receipt records no result for this file",
        )
    recorded = str(integrity.get("status") or "")
    status = INTEGRITY_STATUSES.get(recorded)
    if status is None:
        return (
            "integrity_failed",
            f"integrity_failed: the integrity receipt records {recorded!r}, which is not an "
            "integrity outcome",
        )
    if status == "ok":
        return "ok", ""
    detail = str(integrity.get("detail") or "").strip()
    return status, f"{status}: {detail}" if detail else status


def _size(value: Any) -> str:
    return str(value) if isinstance(value, int) else ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--declared-selection", required=True, type=Path,
        help="the registry's declared ukb-b selection (2,514 rows)",
    )
    parser.add_argument("--source-dir", required=True, type=Path, help="the ukb-b VCF mirror")
    parser.add_argument("--checksums", required=True, type=Path, help="the recorded checksum receipt")
    parser.add_argument("--integrity", required=True, type=Path, help="the recorded integrity receipt")
    parser.add_argument(
        "--verification", required=True, type=Path,
        help="the recorded deep identity re-verification report",
    )
    parser.add_argument("--out", required=True, type=Path, help="the Acquisition Manifest to write")
    args = parser.parse_args(argv)
    try:
        evidence = Evidence(args.checksums, args.integrity, args.verification)
        lineage = write_from_evidence(
            args.declared_selection, args.source_dir, evidence, args.out
        )
        manifest = load_acquisition_manifest(args.out)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Acquisition Manifest {args.out} written for {len(manifest.entries)} Analyses "
        f"({', '.join(f'{status}={count}' for status, count in manifest.readiness_counts.items())}); "
        f"provenance {lineage}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
