#!/usr/bin/env python3
"""ukb-b Source Inventory: reconcile declared intention, local VCFs and the built baseline.

Issue #219. The restored full UKB (`ukb-b`) collection is the one Source
Collection whose intended membership is a *declared selection* rather than an
acquisition-manifest merge: the batch is fixed upstream, and the registry
already records the exact analyses it intends to release
(`resources/inventories/ukb-b-restored-2026-10-11.declared-selection.tsv`, whose
`inclusion_reason` cites the OpenGWAS IGD `data/ukb-b/manifest.tsv`). A
directory glob must never be the intention: if a file is missing, the release
loses a declared member, and that is a copy failure, not a smaller release.

This module therefore:

1. reads the declared selection as intent (refusing duplicate or malformed ids);
2. discovers the local `.vcf.gz` files actually present;
3. reconciles intent against availability and against the old built baseline;
4. freezes a checksummed Source Inventory (resumable, at most two reader
   threads) once -- and only once -- every declared member is verified present;
5. re-verifies a frozen snapshot against the disk.

The freeze hashes 410 GB. It is deliberately capped at two concurrent readers
(the operator's explicit resource grant for this ticket) and writes a resumable
receipt keyed by path + size + mtime, so an interrupted run resumes rather than
re-reads.

It is a pure transform over tracked files and the local mirror: no network, no
`opengwasdb`, no association rows are read. The upstream batch count is recorded
from a supplied evidence file (the unsecured `GET /api/batches` response) rather
than fetched here, so the freeze stays deterministic and offline.

Usage:

    python3 -m manifest_generation.providers.ukb_b.inventory reconcile --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b
    python3 -m manifest_generation.providers.ukb_b.inventory freeze --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b \\
        --snapshot-id ukb-b-restored-2026-10-11 --upstream-batch-evidence <batches.json>
    python3 -m manifest_generation.providers.ukb_b.inventory verify --inventory resources/inventories/ukb-b-restored-2026-10-11.tsv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

from manifest_generation import REPO_ROOT

#: The Source Collection this inventory belongs to (CONTEXT.md).
SOURCE_COLLECTION_ID: str = "opengwas-gwas-vcf"
#: The upstream OpenGWAS batch whose membership the declared selection fixes.
UPSTREAM_BATCH_ID: str = "ukb-b"
#: The normalised ancestry of every Analysis in this batch (source-trusted, no AF).
ANCESTRY_GROUP: str = "EUR"
#: The source file and its index a release consumes.
SOURCE_SUFFIX: str = ".vcf.gz"
INDEX_SUFFIX: str = ".vcf.gz.tbi"
#: The canonical upstream id shape. A file that does not match is reported, never
#: silently coerced into a member.
ANALYSIS_ID_PATTERN = re.compile(r"^ukb-b-\d+$")

#: The declared intended selection for the restored UKB release. It is a Source
#: Inventory input, not a Release Bundle's membership table: the frozen
#: inventory (`ukb-b-restored-2026-10-11`) declares the Selection it was frozen
#: for, and the receipt pins this file's sha256. It moved here from the retired
#: family-first bundle archive in #252; its bytes, and so every recorded hash,
#: are unchanged.
DEFAULT_DECLARED_SELECTION: str = (
    "resources/inventories/ukb-b-restored-2026-10-11.declared-selection.tsv"
)

#: The maximum concurrent readers the operator granted this ticket. A larger
#: request is a configuration error, not a faster run.
MAX_READER_THREADS: int = 2

INVENTORY_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "source_label",
    "source_genome_build",
    "source_file",
    "data_bytes",
    "sha256",
    "checksum_algorithm",
    "mtime_ns",
    "index_file",
    "index_bytes",
    "index_sha256",
    "index_status",
    "readiness_status",
    "exclusion_reason",
)

#: A row is ready when its data file and index are present and its checksum is
#: recorded. Only ready rows may become release members.
READY_STATUSES: frozenset[str] = frozenset({"ok"})
KNOWN_READINESS_STATUSES: frozenset[str] = frozenset(
    {"ok", "missing_file", "missing_index", "size_mismatch", "checksum_mismatch"}
)

RECEIPT_SCHEMA_VERSION: int = 1
INVENTORY_SCHEMA_VERSION: int = 1
INTEGRITY_RECEIPT_SCHEMA_VERSION: int = 1

#: Deep source-integrity outcomes for the BGZF/Tabix check. `ok` means the
#: compressed stream decompresses to EOF, the BGZF EOF marker survives, and the
#: Tabix index opens and lists at least one contig.
INTEGRITY_STATUSES: frozenset[str] = frozenset(
    {"ok", "missing_file", "missing_index", "bad_index", "integrity_failed"}
)

DECLARED_COLUMNS_REQUIRED: tuple[str, ...] = (
    "analysis_id",
    "source_label",
    "source_file",
    "source_genome_build",
)


class InventoryError(ValueError):
    """The intent, the mirror, or a frozen inventory is not usable as declared."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredAnalysis:
    """One Analysis the registry declares it intends to release."""

    analysis_id: str
    source_label: str
    source_genome_build: str
    source_file: str


@dataclass(frozen=True)
class DeclaredSelection:
    """The declared intention, with the provenance that makes it reviewable.

    The declared `source_file` is recorded but never trusted as the local path:
    the freeze resolves each Analysis against `--source-dir` by its id, so a
    declared path on another host cannot masquerade as a local file.
    """

    path: Path
    sha256: str
    rows: tuple[DeclaredAnalysis, ...]

    @property
    def analysis_ids(self) -> tuple[str, ...]:
        return tuple(row.analysis_id for row in self.rows)


@dataclass(frozen=True)
class AvailableSource:
    """One local source file discovered in the mirror."""

    analysis_id: str
    data_file: Path
    data_bytes: int
    mtime_ns: int
    index_file: Path
    index_present: bool


@dataclass(frozen=True)
class BuiltBaseline:
    """A previously built Store Release's membership, for an explicit comparison."""

    store_id: str
    path: Path
    sha256: str
    analysis_ids: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateContentGroup:
    """Analyses whose source files share one checksum: identical bytes, two records."""

    sha256: str
    analysis_ids: tuple[str, ...]
    data_bytes: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "analysis_ids": list(self.analysis_ids),
            "data_bytes": self.data_bytes,
        }


@dataclass(frozen=True)
class Reconciliation:
    """The exact comparison of intent, availability and the built baseline."""

    intended: int
    available: int
    built: int | None
    duplicates_in_selection: tuple[str, ...]
    malformed_selection_ids: tuple[str, ...]
    missing: tuple[str, ...]
    outside_intent: tuple[str, ...]
    unexpected_files: tuple[str, ...]
    index_missing: tuple[str, ...]
    built_only: tuple[str, ...]
    restored_not_built: tuple[str, ...]

    @property
    def exact(self) -> bool:
        """True when availability is exactly the intent and nothing else."""
        return not (
            self.duplicates_in_selection
            or self.malformed_selection_ids
            or self.missing
            or self.outside_intent
            or self.unexpected_files
            or self.index_missing
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "intended": self.intended,
            "available": self.available,
            "built": self.built,
            "restored_not_built": len(self.restored_not_built),
            "duplicates_in_selection": list(self.duplicates_in_selection),
            "malformed_selection_ids": list(self.malformed_selection_ids),
            "missing": list(self.missing),
            "outside_intent": list(self.outside_intent),
            "unexpected_files": list(self.unexpected_files),
            "index_missing": list(self.index_missing),
            "built_only": list(self.built_only),
            "restored_ids": list(self.restored_not_built),
            "exact": self.exact,
        }


@dataclass(frozen=True)
class SourceIdentity:
    """A verified source file identity from the freeze."""

    analysis_id: str
    source_file: Path
    data_bytes: int
    mtime_ns: int
    sha256: str
    index_file: Path
    index_bytes: int
    index_sha256: str


@dataclass(frozen=True)
class FrozenInventory:
    """A frozen Source Inventory and the provenance sidecar that binds it."""

    snapshot_id: str
    rows: tuple[dict[str, str], ...]
    meta: dict[str, object]
    tsv_sha256: str = field(default="")


# ---------------------------------------------------------------------------
# Reading intent and the baseline
# ---------------------------------------------------------------------------


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise InventoryError(f"TSV not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fieldnames = reader.fieldnames or []
        rows = [{name: (row.get(name) or "").strip() for name in fieldnames} for row in reader]
    if not rows:
        raise InventoryError(f"TSV has no rows: {path}")
    return rows


def read_declared_selection(path: Path) -> DeclaredSelection:
    """Read the declared intended membership.

    A duplicate `analysis_id` is a defect in the declaration, because a release
    cannot contain two rows claiming one identity. A malformed id is reported by
    `reconcile` rather than rejected here, so a reviewer sees every problem in
    one pass; it still blocks a freeze.
    """
    rows = _read_tsv(path)
    missing_columns = [name for name in DECLARED_COLUMNS_REQUIRED if name not in rows[0]]
    if missing_columns:
        raise InventoryError(
            f"declared selection {path} is missing required column(s): {', '.join(missing_columns)}"
        )
    declared: list[DeclaredAnalysis] = []
    for row in rows:
        analysis_id = row["analysis_id"]
        if not analysis_id:
            raise InventoryError(f"declared selection {path} has a blank analysis_id")
        declared.append(
            DeclaredAnalysis(
                analysis_id=analysis_id,
                source_label=row["source_label"],
                source_genome_build=row["source_genome_build"],
                source_file=row["source_file"],
            )
        )
    return DeclaredSelection(path=path, sha256=sha256_file(path), rows=tuple(declared))


def read_built_baseline(path: Path, store_id: str = "") -> BuiltBaseline:
    """Read a built bundle's `analyses.tsv` membership for the explicit comparison."""
    rows = _read_tsv(path)
    if "analysis_id" not in rows[0]:
        raise InventoryError(f"built baseline {path} has no analysis_id column")
    ids = tuple(row["analysis_id"] for row in rows)
    return BuiltBaseline(
        store_id=store_id or path.parent.name,
        path=path,
        sha256=sha256_file(path),
        analysis_ids=ids,
    )


# ---------------------------------------------------------------------------
# Discovering the mirror
# ---------------------------------------------------------------------------


def discover_source_collection(source_dir: Path) -> tuple[dict[str, AvailableSource], tuple[str, ...]]:
    """Discover local `.vcf.gz` files, keyed by their id.

    Returns ``(sources, unexpected_files)``. A filename that is not
    `<ukb-b-id>.vcf.gz` is reported as unexpected rather than parsed loosely:
    an unparseable name is not evidence of any Analysis's identity.
    """
    if not source_dir.is_dir():
        raise InventoryError(f"source directory not found: {source_dir}")
    sources: dict[str, AvailableSource] = {}
    unexpected: list[str] = []
    for entry in sorted(source_dir.iterdir()):
        if not entry.name.endswith(SOURCE_SUFFIX):
            continue
        if not entry.is_file():
            unexpected.append(entry.name)
            continue
        analysis_id = entry.name[: -len(SOURCE_SUFFIX)]
        if not ANALYSIS_ID_PATTERN.fullmatch(analysis_id):
            unexpected.append(entry.name)
            continue
        index_file = entry.with_name(entry.name + ".tbi")
        stat = entry.stat()
        sources[analysis_id] = AvailableSource(
            analysis_id=analysis_id,
            data_file=entry,
            data_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            index_file=index_file,
            index_present=index_file.is_file(),
        )
    return sources, tuple(unexpected)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _duplicate_ids(ids: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in ids:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def reconcile(
    selection: DeclaredSelection,
    sources: Mapping[str, AvailableSource],
    unexpected_files: Sequence[str] = (),
    baseline: BuiltBaseline | None = None,
) -> Reconciliation:
    """Compare intent, availability and (optionally) the old built baseline.

    Nothing here treats a discovered file as intent: `outside_intent` and
    `unexpected_files` are errors, not additions. `restored_not_built` is the
    set the old incomplete release never contained -- the delta the restored
    release must add.
    """
    intended_ids = list(selection.analysis_ids)
    duplicates = _duplicate_ids(intended_ids)
    intended = set(intended_ids)
    available = set(sources)
    malformed = tuple(sorted(i for i in intended if not ANALYSIS_ID_PATTERN.fullmatch(i)))
    missing = tuple(sorted(intended - available))
    outside = tuple(sorted(available - intended))
    index_missing = tuple(sorted(i for i, src in sources.items() if i in intended and not src.index_present))

    built_only: tuple[str, ...] = ()
    restored_not_built: tuple[str, ...] = ()
    built_count: int | None = None
    if baseline is not None:
        built = set(baseline.analysis_ids)
        built_count = len(built)
        built_only = tuple(sorted(built - intended))
        restored_not_built = tuple(sorted(intended - built))

    return Reconciliation(
        intended=len(intended),
        available=len(available),
        built=built_count,
        duplicates_in_selection=duplicates,
        malformed_selection_ids=malformed,
        missing=missing,
        outside_intent=outside,
        unexpected_files=tuple(unexpected_files),
        index_missing=index_missing,
        built_only=built_only,
        restored_not_built=restored_not_built,
    )


def render_reconciliation_summary(report: Reconciliation) -> str:
    """Render the operator-facing reconciliation summary."""
    lines = [
        f"declared intent      {report.intended} Analyses",
        f"available on disk    {report.available}",
    ]
    if report.built is not None:
        lines.append(f"old built baseline   {report.built}")
        lines.append(f"restored not built   {len(report.restored_not_built)}")
    for label, values in (
        ("duplicate ids in declaration", report.duplicates_in_selection),
        ("malformed declared ids", report.malformed_selection_ids),
        ("missing from disk", report.missing),
        ("on disk but outside intent", report.outside_intent),
        ("unexpected filenames", report.unexpected_files),
        ("missing index", report.index_missing),
        ("built but not intended", report.built_only),
    ):
        if values:
            shown = ", ".join(values[:10])
            suffix = "" if len(values) <= 10 else f" (+{len(values) - 10} more)"
            lines.append(f"  {label}: {len(values)}: {shown}{suffix}")
    lines.append(
        "OK: intent, availability and baseline reconcile exactly."
        if report.exact
        else "FAILED: reconciliation is not exact; a partial copy must not be frozen."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Checksums (resumable, bounded readers)
# ---------------------------------------------------------------------------


def sha256_file(path: Path, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_receipt(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"checksum receipt is unreadable: {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise InventoryError(f"checksum receipt {path} has an unsupported schema_version")
    entries = document.get("entries")
    return entries if isinstance(entries, dict) else {}


def _write_receipt(path: Path, entries: Mapping[str, Mapping[str, object]]) -> None:
    document = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "entries": {key: dict(value) for key, value in sorted(entries.items())},
    }
    _atomic_write(path, json.dumps(document, indent=1, sort_keys=True) + "\n")


def hash_sources(
    sources: Mapping[str, AvailableSource],
    *,
    workers: int = MAX_READER_THREADS,
    receipt: Path | None = None,
    resume: bool = True,
) -> tuple[dict[str, SourceIdentity], int]:
    """Checksum every source, at most ``workers`` readers at a time.

    A receipt entry is reused only when its recorded ``data_bytes`` and
    ``mtime_ns`` still match the file, so a reused checksum is bound to the same
    bytes. The receipt is rewritten atomically after every completed file, so a
    crash resumes instead of re-reading 410 GB. Returns ``(identities, n_reused)``.
    """
    if workers < 1 or workers > MAX_READER_THREADS:
        raise InventoryError(
            f"workers must be between 1 and {MAX_READER_THREADS} (this ticket's reader grant); got {workers}"
        )
    entries = _read_receipt(receipt) if (receipt and resume) else {}
    identities: dict[str, SourceIdentity] = {}
    reused = 0

    ordered = [sources[key] for key in sorted(sources)]

    def fingerprint(source: AvailableSource) -> dict[str, object]:
        recorded = entries.get(source.analysis_id)
        if (
            isinstance(recorded, dict)
            and recorded.get("data_file") == str(source.data_file)
            and recorded.get("data_bytes") == source.data_bytes
            and recorded.get("mtime_ns") == source.mtime_ns
            and isinstance(recorded.get("sha256"), str)
            and len(str(recorded["sha256"])) == 64
        ):
            return {"source": source, "reused": True, "sha256": str(recorded["sha256"])}
        return {"source": source, "reused": False, "sha256": sha256_file(source.data_file)}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(fingerprint, ordered):
            source: AvailableSource = result["source"]
            after = source.data_file.stat()
            if after.st_size != source.data_bytes or after.st_mtime_ns != source.mtime_ns:
                raise InventoryError(
                    f"{source.data_file} changed while checksumming; re-freeze when the copy is stable"
                )
            if result["reused"]:
                reused += 1
            else:
                entries[source.analysis_id] = {
                    "data_file": str(source.data_file),
                    "data_bytes": source.data_bytes,
                    "mtime_ns": source.mtime_ns,
                    "sha256": result["sha256"],
                }
                if receipt is not None:
                    _write_receipt(receipt, entries)
            identities[source.analysis_id] = SourceIdentity(
                analysis_id=source.analysis_id,
                source_file=source.data_file,
                data_bytes=source.data_bytes,
                mtime_ns=source.mtime_ns,
                sha256=result["sha256"],
                index_file=source.index_file,
                index_bytes=source.index_file.stat().st_size if source.index_file.is_file() else 0,
                index_sha256=sha256_file(source.index_file) if source.index_file.is_file() else "",
            )
    return identities, reused


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------


def _duplicate_content_groups(
    identities: Mapping[str, SourceIdentity],
) -> tuple[DuplicateContentGroup, ...]:
    by_checksum: dict[str, list[SourceIdentity]] = {}
    for identity in identities.values():
        by_checksum.setdefault(identity.sha256, []).append(identity)
    groups = [
        DuplicateContentGroup(
            sha256=checksum,
            analysis_ids=tuple(sorted(i.analysis_id for i in members)),
            data_bytes=max(i.data_bytes for i in members),
        )
        for checksum, members in by_checksum.items()
        if len(members) > 1
    ]
    groups.sort(key=lambda group: group.analysis_ids[0])
    return tuple(groups)


def build_inventory_rows(
    selection: DeclaredSelection,
    identities: Mapping[str, SourceIdentity],
) -> tuple[dict[str, str], ...]:
    """Render one inventory row per declared Analysis, in declared order."""
    rows: list[dict[str, str]] = []
    for declared in selection.rows:
        identity = identities.get(declared.analysis_id)
        if identity is None:
            raise InventoryError(
                f"{declared.analysis_id} has no verified source identity; freeze requires every declared member"
            )
        rows.append(
            {
                "analysis_id": declared.analysis_id,
                "source_label": declared.source_label,
                "source_genome_build": declared.source_genome_build,
                "source_file": str(identity.source_file),
                "data_bytes": str(identity.data_bytes),
                "sha256": identity.sha256,
                "checksum_algorithm": "sha256",
                "mtime_ns": str(identity.mtime_ns),
                "index_file": str(identity.index_file),
                "index_bytes": str(identity.index_bytes),
                "index_sha256": identity.index_sha256,
                "index_status": "ok" if identity.index_file.is_file() else "missing",
                "readiness_status": "ok",
                "exclusion_reason": "",
            }
        )
    return tuple(rows)


def render_inventory_tsv(rows: Sequence[Mapping[str, str]]) -> str:
    lines = ["\t".join(INVENTORY_COLUMNS)]
    for row in rows:
        for name in INVENTORY_COLUMNS:
            value = row.get(name, "")
            if "\t" in value or "\n" in value:
                raise InventoryError(f"{row.get('analysis_id', '?')}: {name} contains a tab or newline")
        lines.append("\t".join(row.get(name, "") for name in INVENTORY_COLUMNS))
    return "\n".join(lines) + "\n"


def _read_upstream_batch_evidence(path: Path | None, batch_id: str) -> dict[str, object]:
    """Read the unsecured `GET /api/batches` evidence for one batch, if supplied.

    Recorded, never fetched: the freeze must stay deterministic and offline. The
    evidence file is the operator's saved API response.
    """
    if path is None:
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"upstream batch evidence is unreadable: {path}: {exc}") from exc
    if not isinstance(document, list):
        raise InventoryError(f"upstream batch evidence {path} is not a JSON list")
    for entry in document:
        if isinstance(entry, dict) and entry.get("id") == batch_id:
            return {
                "id": batch_id,
                "count": int(entry.get("count", 0)),
                "description": str(entry.get("description", "")),
                "evidence_path": str(path),
                "evidence_sha256": sha256_file(path),
            }
    raise InventoryError(f"upstream batch evidence {path} has no batch {batch_id!r}")


def freeze(
    *,
    selection: DeclaredSelection,
    sources: Mapping[str, AvailableSource],
    unexpected_files: Sequence[str],
    snapshot_id: str,
    baseline: BuiltBaseline | None = None,
    upstream_batch_evidence: Path | None = None,
    workers: int = MAX_READER_THREADS,
    receipt: Path | None = None,
    frozen_at: str | None = None,
) -> tuple[FrozenInventory, Reconciliation, int]:
    """Freeze the Source Inventory, refusing to freeze anything but the exact intent.

    Returns ``(inventory, reconciliation, reused_checksums)``. The freeze fails
    before hashing a single byte when reconciliation is not exact, so a partial
    copy can never be recorded as the frozen input set.
    """
    report = reconcile(selection, sources, unexpected_files, baseline)
    if not report.exact:
        raise InventoryError(
            "refusing to freeze: declared intent does not reconcile against the mirror "
            "(a partial copy must not be accepted)\n" + render_reconciliation_summary(report)
        )
    identities, reused = hash_sources(sources, workers=workers, receipt=receipt)
    rows = build_inventory_rows(selection, identities)
    ready_bytes = sum(identity.data_bytes for identity in identities.values())
    groups = _duplicate_content_groups(identities)
    tsv = render_inventory_tsv(rows)
    tsv_sha256 = hashlib.sha256(tsv.encode("utf-8")).hexdigest()

    upstream = _read_upstream_batch_evidence(upstream_batch_evidence, UPSTREAM_BATCH_ID)
    if upstream and upstream["count"] != report.intended:
        raise InventoryError(
            f"upstream batch {UPSTREAM_BATCH_ID} reports {upstream['count']} Analyses but the declared "
            f"selection has {report.intended}; the declaration is not the whole batch"
        )

    meta: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "inventory_schema_version": INVENTORY_SCHEMA_VERSION,
        "source_collection_id": SOURCE_COLLECTION_ID,
        "store_key": UPSTREAM_BATCH_ID,
        "ancestry_group": ANCESTRY_GROUP,
        "frozen_at": frozen_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": len(rows),
        "ready_rows": len(rows),
        "ready_bytes": ready_bytes,
        "ready_index_bytes": sum(identity.index_bytes for identity in identities.values()),
        "checksum_algorithm": "sha256",
        "index_checksum_algorithm": "sha256",
        "upstream_batch": upstream,
        "declared_selection": {
            "path": str(selection.path),
            "sha256": selection.sha256,
            "rows": len(selection.rows),
        },
        "built_baseline": (
            {
                "store_id": baseline.store_id,
                "path": str(baseline.path),
                "sha256": baseline.sha256,
                "rows": len(baseline.analysis_ids),
            }
            if baseline is not None
            else {}
        ),
        "restored_not_built": len(report.restored_not_built),
        "duplicate_content_groups": [group.as_dict() for group in groups],
        "inventory_tsv_sha256": tsv_sha256,
    }
    return FrozenInventory(snapshot_id=snapshot_id, rows=rows, meta=meta, tsv_sha256=tsv_sha256), report, reused


def write_inventory(inventory: FrozenInventory, directory: Path) -> tuple[Path, Path]:
    """Write `<snapshot-id>.tsv` and `<snapshot-id>.meta.yaml` atomically."""
    directory.mkdir(parents=True, exist_ok=True)
    tsv_path = directory / f"{inventory.snapshot_id}.tsv"
    meta_path = directory / f"{inventory.snapshot_id}.meta.yaml"
    _atomic_write(tsv_path, render_inventory_tsv(inventory.rows))
    _atomic_write(meta_path, yaml.safe_dump(inventory.meta, sort_keys=False, default_flow_style=False, width=100))
    return tsv_path, meta_path


# ---------------------------------------------------------------------------
# Verifying a frozen inventory
# ---------------------------------------------------------------------------


def read_inventory(path: Path) -> tuple[dict[str, str], ...]:
    rows = _read_tsv(path)
    if "analysis_id" not in rows[0] or "sha256" not in rows[0]:
        raise InventoryError(f"inventory {path} is missing analysis_id/sha256 columns")
    duplicates = _duplicate_ids(row["analysis_id"] for row in rows)
    if duplicates:
        raise InventoryError(f"inventory {path} has duplicate analysis_id: {', '.join(duplicates)}")
    return tuple(rows)


def verify_inventory(
    path: Path,
    *,
    deep: bool = False,
    workers: int = MAX_READER_THREADS,
) -> dict[str, object]:
    """Re-check a frozen inventory against the disk.

    Shallow by default: presence, size and mtime. ``deep`` additionally re-reads
    and re-checksums every file (bounded readers), which is the only way to
    detect same-size same-mtime content substitution.
    """
    rows = read_inventory(path)
    missing: list[str] = []
    size_mismatch: list[str] = []
    mtime_changed: list[str] = []
    checksum_mismatch: list[str] = []
    index_mismatch: list[str] = []
    to_hash: list[tuple[str, Path, str]] = []
    for row in rows:
        source = Path(row["source_file"])
        if not source.is_file():
            missing.append(row["analysis_id"])
            continue
        stat = source.stat()
        if stat.st_size != int(row["data_bytes"]):
            size_mismatch.append(f"{row['analysis_id']} ({stat.st_size} != {row['data_bytes']})")
            continue
        if stat.st_mtime_ns != int(row["mtime_ns"]):
            mtime_changed.append(row["analysis_id"])
        # The Tabix index identity is frozen too and is re-verified independently
        # of the (larger) data checksum scan.
        if "index_sha256" in row:
            index = Path(row.get("index_file", ""))
            if not index.is_file():
                index_mismatch.append(f"{row['analysis_id']} (index missing: {index})")
            elif row.get("index_bytes", "").isdigit() and index.stat().st_size != int(row["index_bytes"]):
                index_mismatch.append(f"{row['analysis_id']} (index size changed)")
            elif sha256_file(index) != row.get("index_sha256", ""):
                index_mismatch.append(f"{row['analysis_id']} (index checksum mismatch)")
        to_hash.append((row["analysis_id"], source, row["sha256"]))
    if deep and to_hash:
        with ThreadPoolExecutor(max_workers=min(workers, MAX_READER_THREADS)) as executor:
            digests = list(executor.map(lambda item: sha256_file(item[1]), to_hash))
        checksum_mismatch = [
            f"{analysis_id} ({digest} != {expected})"
            for (analysis_id, _source, expected), digest in zip(to_hash, digests)
            if digest != expected
        ]
    report: dict[str, object] = {
        "inventory": str(path),
        "rows": len(rows),
        "deep": deep,
        "missing": sorted(missing),
        "size_mismatch": sorted(size_mismatch),
        "mtime_changed": sorted(mtime_changed),
        "checksum_mismatch": sorted(checksum_mismatch),
        "index_mismatch": sorted(index_mismatch),
    }
    report["ok"] = not (missing or size_mismatch or checksum_mismatch or index_mismatch)
    return report


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Deep BGZF / Tabix integrity
# ---------------------------------------------------------------------------


def _bgzip_test(path: Path) -> str:
    """Decompress a BGZF file to EOF with the `bgzip` binary when present."""
    result = subprocess.run(
        ["bgzip", "-t", str(path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        return (result.stderr or result.stdout or "bgzip -t failed").strip().splitlines()[0]
    return ""


def _pysam_test(path: Path) -> str:
    """Fallback full BGZF decompression using the pinned environment's pysam."""
    import pysam  # imported lazily so the rest of the module needs no pysam

    try:
        with pysam.BGZFile(str(path), "rb") as handle:
            while handle.read(1 << 22):
                pass
    except Exception as exc:  # noqa: BLE001 - reported, never raised past the worker
        return f"{type(exc).__name__}: {exc}"
    return ""


def _tabix_contigs(path: Path) -> tuple[list[str], str]:
    """Validate the Tabix index; return ``(contigs, error)``."""
    index = Path(str(path) + ".tbi")
    if not index.is_file():
        return [], "index_missing"
    # A .tbi is itself BGZF-compressed (or raw in older tools), so the magic is
    # not a reliable gate; opening it and listing contigs is the real check.
    try:
        result = subprocess.run(["tabix", "-l", str(path)], capture_output=True, text=True)
    except FileNotFoundError:
        import pysam  # noqa: PLC0415

        with pysam.TabixFile(str(path)) as tabix:
            return list(tabix.contigs), ""
    if result.returncode != 0:
        return [], f"bad_index: {(result.stderr or result.stdout).strip().splitlines()[0] if (result.stderr or result.stdout).strip() else 'tabix -l failed'}"
    return [line for line in result.stdout.split() if line], ""


def _check_integrity_one(item: tuple[str, str]) -> tuple[str, str, str]:
    """Return ``(analysis_id, status, detail)`` for one source file."""
    analysis_id, source_file = item
    path = Path(source_file)
    if not path.is_file():
        return analysis_id, "missing_file", str(path)
    if not Path(str(path) + ".tbi").is_file():
        return analysis_id, "missing_index", str(path) + ".tbi"
    bgzf_error = _bgzip_test(path) if shutil.which("bgzip") else _pysam_test(path)
    if bgzf_error:
        return analysis_id, "integrity_failed", bgzf_error
    try:
        contigs, index_error = _tabix_contigs(path)
    except Exception as exc:  # noqa: BLE001
        return analysis_id, "bad_index", f"{type(exc).__name__}: {exc}"
    if index_error:
        return analysis_id, index_error.split(":", 1)[0], index_error
    if not contigs:
        return analysis_id, "bad_index", "index lists no contigs"
    return analysis_id, "ok", f"contigs={len(contigs)}"


def check_source_integrity(
    rows: Sequence[Mapping[str, str]],
    *,
    workers: int = MAX_READER_THREADS,
    receipt: Path | None = None,
    resume: bool = True,
) -> tuple[dict[str, dict[str, str]], int]:
    """Deeply check every source's BGZF stream and Tabix index, two readers at most.

    The freeze proves the bytes have not changed; this proves the bytes are a
    complete BGZF stream whose Tabix index opens. It is resumable through the
    same size+mtime receipt discipline as the checksum freeze.
    """
    if workers < 1 or workers > MAX_READER_THREADS:
        raise InventoryError(
            f"workers must be between 1 and {MAX_READER_THREADS} (this ticket's reader grant); got {workers}"
        )
    entries = _read_integrity_receipt(receipt) if (receipt and resume) else {}
    results: dict[str, dict[str, str]] = {}
    reused = 0

    pending: list[tuple[str, str]] = []
    for row in rows:
        analysis_id = row["analysis_id"]
        path = Path(row["source_file"])
        stat = path.stat() if path.is_file() else None
        recorded = entries.get(analysis_id)
        if (
            isinstance(recorded, dict)
            and recorded.get("data_bytes") == (stat.st_size if stat else None)
            and recorded.get("mtime_ns") == (stat.st_mtime_ns if stat else None)
            and recorded.get("status") == "ok"
        ):
            results[analysis_id] = {"status": "ok", "detail": str(recorded.get("detail", ""))}
            reused += 1
            continue
        pending.append((analysis_id, str(path)))

    if pending:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for analysis_id, status, detail in executor.map(_check_integrity_one, pending):
                results[analysis_id] = {"status": status, "detail": detail}
                if receipt is not None:
                    row = next(r for r in rows if r["analysis_id"] == analysis_id)
                    path = Path(row["source_file"])
                    stat = path.stat()
                    entries[analysis_id] = {
                        "data_bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "status": status,
                        "detail": detail,
                    }
                    _write_integrity_receipt(receipt, entries)
    return results, reused


def _read_integrity_receipt(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != INTEGRITY_RECEIPT_SCHEMA_VERSION:
        raise InventoryError(f"integrity receipt {path} has an unsupported schema_version")
    entries = document.get("entries")
    return entries if isinstance(entries, dict) else {}


def _write_integrity_receipt(path: Path, entries: Mapping[str, Mapping[str, object]]) -> None:
    document = {
        "schema_version": INTEGRITY_RECEIPT_SCHEMA_VERSION,
        "entries": {key: dict(value) for key, value in sorted(entries.items())},
    }
    _atomic_write(path, json.dumps(document, indent=1, sort_keys=True) + "\n")


def report_source_integrity(results: Mapping[str, Mapping[str, str]]) -> dict[str, object]:
    failures = {aid: r for aid, r in results.items() if r["status"] != "ok"}
    return {
        "rows": len(results),
        "ok": len(results) - len(failures),
        "failures": {aid: {"status": r["status"], "detail": r["detail"]} for aid, r in sorted(failures.items())},
        "passed": not failures,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--source-dir", required=True, help="the ukb-b VCF mirror")
    shared.add_argument(
        "--declared",
        default=DEFAULT_DECLARED_SELECTION,
        help="the declared intended selection (never a glob)",
    )
    shared.add_argument("--built-baseline", help="a built bundle's analyses.tsv for the explicit comparison")
    shared.add_argument("--built-store-id", default="OGS-00009")

    recon = subparsers.add_parser("reconcile", parents=[shared], help="report intent vs availability vs baseline")
    recon.add_argument("--report", help="write the machine-readable JSON report here")
    recon.add_argument("--json", action="store_true", help="print the JSON report instead of the summary")

    freeze = subparsers.add_parser("freeze", parents=[shared], help="checksum and freeze the Source Inventory")
    freeze.add_argument("--snapshot-id", required=True)
    freeze.add_argument("--out-dir", default="resources/inventories")
    freeze.add_argument("--receipt", help="resumable checksum receipt path (default: <out-dir>/<snapshot-id>.checksums.json)")
    freeze.add_argument("--upstream-batch-evidence", help="saved unsecured GET /api/batches JSON")
    freeze.add_argument("--workers", type=int, default=MAX_READER_THREADS)
    freeze.add_argument("--frozen-at")

    verify = subparsers.add_parser("verify", help="re-check a frozen inventory against the disk")
    verify.add_argument("--inventory", required=True)
    verify.add_argument("--deep", action="store_true", help="re-checksum every file (two readers max)")
    verify.add_argument("--workers", type=int, default=MAX_READER_THREADS)
    verify.add_argument("--json", action="store_true")

    integrity = subparsers.add_parser(
        "integrity", help="deep BGZF/Tabix integrity check of a frozen inventory (two readers max)"
    )
    integrity.add_argument("--inventory", required=True)
    integrity.add_argument("--workers", type=int, default=MAX_READER_THREADS)
    integrity.add_argument("--receipt", help="resumable integrity receipt path")
    integrity.add_argument("--report", help="write the machine-readable JSON report here")
    integrity.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _load(args: argparse.Namespace, repo_root: Path) -> tuple[DeclaredSelection, dict[str, AvailableSource], tuple[str, ...], BuiltBaseline | None]:
    declared_path = Path(args.declared)
    if not declared_path.is_absolute():
        declared_path = repo_root / declared_path
    selection = read_declared_selection(declared_path)
    sources, unexpected = discover_source_collection(Path(args.source_dir))
    baseline = None
    if args.built_baseline:
        baseline = read_built_baseline(Path(args.built_baseline), args.built_store_id)
    return selection, sources, unexpected, baseline


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root)

    if args.command == "verify":
        report = verify_inventory(Path(args.inventory), deep=args.deep, workers=args.workers)
        print(json.dumps(report, indent=1, sort_keys=True) if args.json else render_verify_summary(report))
        return 0 if report["ok"] else 1

    if args.command == "integrity":
        rows = read_inventory(Path(args.inventory))
        receipt = Path(args.receipt) if args.receipt else None
        results, reused = check_source_integrity(rows, workers=args.workers, receipt=receipt)
        report = report_source_integrity(results)
        report["reused"] = reused
        if args.report:
            _atomic_write(Path(args.report), json.dumps(report, indent=1, sort_keys=True) + "\n")
        print(json.dumps(report, indent=1, sort_keys=True) if args.json else render_integrity_summary(report))
        return 0 if report["passed"] else 1

    selection, sources, unexpected, baseline = _load(args, repo_root)
    report = reconcile(selection, sources, unexpected, baseline)

    if args.command == "reconcile":
        payload = report.as_dict()
        if args.report:
            _atomic_write(Path(args.report), json.dumps(payload, indent=1, sort_keys=True) + "\n")
        print(json.dumps(payload, indent=1, sort_keys=True) if args.json else render_reconciliation_summary(report))
        return 0 if report.exact else 1

    # freeze
    started = time.monotonic()
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = repo_root / out_dir
    receipt = Path(args.receipt) if args.receipt else out_dir / f"{args.snapshot_id}.checksums.json"
    inventory, report, reused = freeze(
        selection=selection,
        sources=sources,
        unexpected_files=unexpected,
        snapshot_id=args.snapshot_id,
        baseline=baseline,
        upstream_batch_evidence=Path(args.upstream_batch_evidence) if args.upstream_batch_evidence else None,
        workers=args.workers,
        receipt=receipt,
        frozen_at=args.frozen_at,
    )
    tsv_path, meta_path = write_inventory(inventory, out_dir)
    elapsed = time.monotonic() - started
    print(render_reconciliation_summary(report))
    print(
        f"Froze {len(inventory.rows)} Analyses ({inventory.meta['ready_bytes']} bytes) "
        f"to {tsv_path} (reused {reused} checksums, {elapsed:.1f}s)"
    )
    print(f"Provenance: {meta_path}")
    return 0


def render_verify_summary(report: Mapping[str, object]) -> str:
    lines = [
        f"inventory {report['inventory']} ({report['rows']} rows, deep={report['deep']})",
        f"  missing {len(report['missing'])}",  # type: ignore[arg-type]
        f"  size mismatch {len(report['size_mismatch'])}",  # type: ignore[arg-type]
        f"  mtime changed {len(report['mtime_changed'])}",  # type: ignore[arg-type]
        f"  checksum mismatch {len(report['checksum_mismatch'])}\n",  # type: ignore[arg-type]
        f"  index mismatch {len(report.get('index_mismatch', []))}",  # type: ignore[arg-type]
    ]
    lines.append("OK: frozen inventory matches the disk." if report["ok"] else "FAILED: frozen inventory no longer matches the disk.")
    return "\n".join(lines)


def render_integrity_summary(report: Mapping[str, object]) -> str:
    lines = [
        f"BGZF/Tabix integrity: {report['ok']}/{report['rows']} source files pass",
    ]
    for analysis_id, failure in (report.get("failures") or {}).items():
        lines.append(f"  {analysis_id}: {failure['status']} ({failure['detail']})")
    lines.append("OK: every source decompresses to EOF and its Tabix index opens." if report["passed"] else "FAILED: source integrity is not clean.")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
