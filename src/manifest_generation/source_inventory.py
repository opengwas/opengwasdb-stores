"""Freeze and preflight an acquired Source Inventory (issue #151).

Phase B selects Analyses out of a **Source Inventory**: the frozen record of
which upstream Analyses acquisition actually produced usable files for. This
module is the seam for that record. It owns three things and nothing else:

- **the pass-overlay precedence rule** — every acquisition pass writes a status
  manifest, and the release config declares those passes as an *ordered* list
  whose order is the precedence rule: a later pass is authoritative for every
  ``analysis_id`` it covers, because it is the later observation of the same
  file. A later pass may not, however, turn a ready Analysis into a non-ready
  one: that would silently drop a release member, so the freeze stops instead;
- **the readiness vocabulary** — which acquisition outcomes mean "this Analysis
  has a verified, readable source file" and which mean "not this release's
  member", kept distinct so an unavailable input stays a Source Inventory fact
  and never looks like a selected member;
- **preflight** — the cheap proof, before any association row is read, that the
  frozen snapshot still describes the mirror on disk, that its declared
  Reference Resources exist, and that the method tiers the release plans to
  apply are declared rather than guessed.

What it deliberately does **not** do: read a full GWAS-SSF association body,
recompute a source checksum, compute ancestry or effect scale, or decide which
duplicate-content accession to build. Preflight compares recorded *sizes*, not
checksums, because checksumming 1.8 TB is not a preflight — that belongs to the
resolution stage. Duplicate-content groups are reported for review and never
silently collapsed.

The Phase B ``preflight`` command is an inventory-readiness gate before candidate
selection, distinct from the Phase A production workflow's **Preflight Run**
(``CONTEXT.md``).

See ``docs/release-metadata-schema.md`` ("Source Inventory") for the column
contract and ``docs/adr/0024-one-family-record-no-source-collection-tier.md``
for why the inventory is a data file under ``resources/inventories/`` rather
than a metadata tier.
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

#: Acquisition outcomes that mean "this Analysis has a verified source file".
#:
#: ``already_present`` is as trustworthy as ``ok``: the downloader skips the
#: transfer, then still runs the metadata gate, the GWAS-SSF header gate and the
#: checksum. ``dry_run`` resolves remote names but downloads nothing, so it is
#: never ready.
READY_STATUSES: frozenset[str] = frozenset({"ok", "already_present"})

#: Every status the acquisition script's manifest can carry. A status outside
#: this set fails loudly rather than being classified as unavailable by default,
#: because "unknown status" and "known-unusable" are different facts.
KNOWN_READINESS_STATUSES: frozenset[str] = frozenset(
    {
        "ok",
        "already_present",
        "missing_remote_harmonised_yaml",
        "header_rejected",
        "metadata_rejected",
        # A resolved filename whose data file upstream serves as HTTP 404:
        # an orphan sidecar, or an index entry for a withdrawn file. Distinct
        # from ``data_failed``, which is a transfer worth retrying.
        "data_absent_upstream",
        "data_failed",
        "yaml_failed",
        "dry_run",
        "error",
    }
)

#: Acquisition manifest columns this module reads. ``seconds`` is deliberately
#: not among them: it is per-run download timing, not a release fact, and
#: carrying it would make the frozen inventory depend on how fast the mirror was.
ACQUISITION_MANIFEST_COLUMNS: tuple[str, ...] = (
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
    "error",
)

#: Candidate-table columns the freeze needs: identity and the selection scope.
CANDIDATE_COLUMNS: tuple[str, ...] = ("STUDY.ACCESSION", "store_key", "study_design")

#: Frozen Source Inventory columns, in file order.
INVENTORY_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "publication_pmid",
    "trait",
    "study_design",
    "sample_size",
    "readiness_status",
    "data_url",
    "yaml_url",
    "data_file",
    "yaml_file",
    "data_bytes",
    "yaml_bytes",
    "sha256",
    "error",
)

#: The assembly every source in this collection is required to declare, and the
#: harmonisation flag the acquisition script gated on. Re-asserted at preflight
#: so a substituted or replaced file cannot pass as the frozen one.
REQUIRED_SOURCE_ASSEMBLY: str = "GRCh38"

#: Issue #150 caps the release at 64 cores; a larger request is a configuration
#: error, not a faster run.
MAX_CORES: int = 64

PREFLIGHT_VERSION: int = 1


class InventoryError(ValueError):
    """A frozen Source Inventory or its inputs are not usable as declared."""


class PreflightConfigError(InventoryError):
    """A full-release generator config is missing or contradicting a required fact."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceInventoryRow:
    """One discovered Analysis, with its recorded readiness and source identity."""

    analysis_id: str
    publication_pmid: str
    trait: str
    study_design: str
    sample_size: str
    readiness_status: str
    data_url: str
    yaml_url: str
    data_file: str
    yaml_file: str
    data_bytes: str
    yaml_bytes: str
    sha256: str
    error: str

    @property
    def ready(self) -> bool:
        """True when acquisition produced a verified source file for this Analysis."""
        return self.readiness_status in READY_STATUSES

    @property
    def recorded_bytes(self) -> int | None:
        """The recorded compressed size, or ``None`` when acquisition recorded none."""
        value = self.data_bytes.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise InventoryError(
                f"{self.analysis_id}: data_bytes is not an integer: {value!r}"
            ) from exc

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in INVENTORY_COLUMNS}


@dataclass(frozen=True)
class ManifestInput:
    """One acquisition manifest that contributed rows to the snapshot."""

    role: str
    path: Path
    sha256: str
    bytes: int
    rows: int
    readiness_counts: Mapping[str, int]
    overrides: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": str(self.path),
            "sha256": self.sha256,
            "bytes": self.bytes,
            "rows": self.rows,
            "readiness_counts": dict(self.readiness_counts),
            "overrides": self.overrides,
        }


@dataclass(frozen=True)
class AcquisitionPass:
    """One acquisition pass a freeze overlays, and the role it plays in the merge.

    A release declares its passes as an ordered sequence, earliest first. Order is
    the whole precedence rule: for an ``analysis_id`` two passes both cover, the
    later pass wins.
    """

    role: str
    path: Path


@dataclass(frozen=True)
class DuplicateContentGroup:
    """Analyses whose source files share one checksum: identical bytes, two records."""

    sha256: str
    analysis_ids: tuple[str, ...]
    data_bytes: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "analysis_ids": list(self.analysis_ids),
            "data_bytes": self.data_bytes,
        }


@dataclass(frozen=True)
class CandidateSelection:
    """The candidate-table rows a freeze accounts the acquisition manifests against."""

    path: Path
    sha256: str
    bytes: int
    store_key: str
    analysis_ids: tuple[str, ...]
    study_design_counts: Mapping[str, int]


@dataclass(frozen=True)
class InventorySnapshot:
    """A frozen Source Inventory plus the provenance that makes it reviewable."""

    snapshot_id: str
    source_collection_id: str
    store_key: str
    ancestry_group: str
    rows: tuple[SourceInventoryRow, ...]
    inputs: tuple[ManifestInput, ...]
    candidates: CandidateSelection
    frozen_at: str

    @property
    def readiness_counts(self) -> dict[str, int]:
        return _counts(row.readiness_status for row in self.rows)

    @property
    def ready_rows(self) -> tuple[SourceInventoryRow, ...]:
        return tuple(row for row in self.rows if row.ready)

    @property
    def ready_bytes(self) -> int:
        total = 0
        for row in self.ready_rows:
            bytes_val = row.recorded_bytes
            if bytes_val is None:
                raise InventoryError(f"{row.analysis_id}: ready row has no recorded data_bytes")
            total += bytes_val
        return total

    @property
    def study_design_counts(self) -> dict[str, int]:
        return _counts(row.study_design for row in self.rows)

    @property
    def ready_study_design_counts(self) -> dict[str, int]:
        return _counts(row.study_design for row in self.ready_rows)

    @property
    def duplicates(self) -> tuple[DuplicateContentGroup, ...]:
        return duplicate_content_groups(self.rows)


# ---------------------------------------------------------------------------
# Reading and merging acquisition manifests
# ---------------------------------------------------------------------------


def _read_tsv(path: Path, required_columns: Sequence[str], what: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise InventoryError(f"{what} not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fieldnames = reader.fieldnames or []
        missing = [name for name in required_columns if name not in fieldnames]
        if missing:
            raise InventoryError(
                f"{what} {path} is missing required column(s): {', '.join(missing)}"
            )
        rows = [{name: (row.get(name) or "").strip() for name in fieldnames} for row in reader]
    if not rows:
        raise InventoryError(f"{what} {path} has no rows")
    return rows


def _duplicate_ids(rows: Iterable[Mapping[str, str]], key: str) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        value = row[key]
        if value in seen:
            duplicates.append(value)
        seen.add(value)
    return sorted(set(duplicates))


def read_acquisition_manifest(path: Path) -> list[dict[str, str]]:
    """Read one acquisition status manifest, failing on duplicate or unknown rows.

    Duplicate ``analysis_id`` inside a single manifest is a defect in that
    manifest: the merge rule (a later manifest overrides an earlier one) cannot
    resolve two rows that claim the same identity in the same pass.
    """
    rows = _read_tsv(path, ACQUISITION_MANIFEST_COLUMNS, "acquisition manifest")
    duplicates = _duplicate_ids(rows, "analysis_id")
    if duplicates:
        raise InventoryError(
            f"acquisition manifest {path} has duplicate analysis_id: {', '.join(duplicates)}"
        )
    unknown = sorted({row["status"] for row in rows} - KNOWN_READINESS_STATUSES)
    if unknown:
        raise InventoryError(
            f"acquisition manifest {path} has unknown readiness status: {', '.join(unknown)}"
        )
    return rows


def merge_acquisition_manifests(
    passes: Sequence[tuple[str, Sequence[Mapping[str, str]]]],
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Overlay ordered acquisition passes; return ``(merged, overrides_by_role)``.

    ``passes`` is ``(role, rows)`` in precedence order, earliest first — the order
    the release config declares. Each later pass is the later observation of the
    same upstream file, so it wins for every ``analysis_id`` it covers. Every row
    a pass overrides is counted against that pass's role, so how much each pass
    moved the frozen snapshot stays visible in the provenance.
    """
    merged: dict[str, dict[str, str]] = {}
    overrides_by_role: dict[str, int] = {}
    for role, rows in passes:
        overridden = 0
        for row in rows:
            analysis_id = row["analysis_id"]
            if analysis_id in merged:
                overridden += 1
            merged[analysis_id] = dict(row)
        overrides_by_role[role] = overrides_by_role.get(role, 0) + overridden
    return [merged[key] for key in sorted(merged)], overrides_by_role


def reject_ready_regressions(
    passes: Sequence[tuple[str, Sequence[Mapping[str, str]]]],
    merged: Mapping[str, Mapping[str, str]],
) -> None:
    """Fail when a later pass turns a ready Analysis into a non-ready one.

    Overlaying later passes is safe because a later pass is the later observation
    of the same upstream file — but "later" is not a licence to discard a verified
    source file. A pass that regressed an already-ready row would silently drop a
    release member, so this is an error rather than a precedence rule (issue #151).
    """
    ready_status: dict[str, str] = {}
    last_writer: dict[str, str] = {}
    for role, rows in passes:
        for row in rows:
            analysis_id = row["analysis_id"]
            last_writer[analysis_id] = role
            if row["status"] in READY_STATUSES:
                ready_status.setdefault(analysis_id, row["status"])
    regressions = [
        (analysis_id, status, merged[analysis_id]["status"], last_writer[analysis_id])
        for analysis_id, status in sorted(ready_status.items())
        if merged[analysis_id]["status"] not in READY_STATUSES
    ]
    if not regressions:
        return
    detail = "; ".join(
        f"{analysis_id} ({status} -> {merged_status} by {role})"
        for analysis_id, status, merged_status, role in regressions[:10]
    )
    raise InventoryError(
        f"{len(regressions)} Analysis(es) were ready in an earlier acquisition pass but a later "
        f"pass recorded them as non-ready: {detail}"
    )


def _readiness_counts(rows: Iterable[Mapping[str, str]]) -> dict[str, int]:
    return _counts(row["status"] for row in rows)


# ---------------------------------------------------------------------------
# Candidate accounting
# ---------------------------------------------------------------------------


def read_candidate_selection(
    path: Path, store_key: str, *, sha256: str | None = None, size: int | None = None
) -> CandidateSelection:
    """Read the candidate table's rows for one ``store_key``.

    This is the pool acquisition was asked to fetch, so it is the only thing the
    merged manifests can be accounted against: a candidate with no manifest row
    was never attempted, and a manifest row outside the pool came from somewhere
    else. Both are errors, not warnings.
    """
    if not path.is_file():
        raise InventoryError(
            f"candidate table not found: {path}. It is generated data that is not tracked in git; "
            "run `Rscript resources/scripts/ebi-studies.r` first, or pass --candidates with the "
            "copy this host generated."
        )
    rows = _read_tsv(path, CANDIDATE_COLUMNS, "candidate table")
    selected = [row for row in rows if row["store_key"] == store_key]
    if not selected:
        raise InventoryError(f"candidate table {path} has no rows for store_key {store_key!r}")
    duplicates = _duplicate_ids(selected, "STUDY.ACCESSION")
    if duplicates:
        raise InventoryError(
            f"candidate table {path} selects duplicate STUDY.ACCESSION for {store_key!r}: "
            f"{', '.join(duplicates)}"
        )
    return CandidateSelection(
        path=path,
        sha256=sha256 if sha256 is not None else sha256_file(path),
        bytes=size if size is not None else path.stat().st_size,
        store_key=store_key,
        analysis_ids=tuple(sorted(row["STUDY.ACCESSION"] for row in selected)),
        study_design_counts=_counts(row["study_design"] for row in selected),
    )


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------


def build_snapshot(
    *,
    snapshot_id: str,
    source_collection_id: str,
    store_key: str,
    ancestry_group: str,
    manifests: Sequence[AcquisitionPass],
    candidates: CandidateSelection,
    frozen_at: str | None = None,
) -> InventorySnapshot:
    """Merge the ordered acquisition passes and account every row against the candidates.

    ``manifests`` is in precedence order, earliest first. Deterministic by
    construction: rows are sorted by ``analysis_id``, the ``seconds`` column is
    dropped, and nothing here reads the mirror. Two freezes of the same inputs
    produce the same inventory bytes; only ``frozen_at`` differs, and it lives in
    the provenance sidecar rather than the TSV.
    """
    if not manifests:
        raise InventoryError(
            "a freeze needs at least one acquisition manifest; source.inventory.freeze_inputs "
            "declares the ordered passes"
        )
    passes = [(manifest.role, read_acquisition_manifest(manifest.path)) for manifest in manifests]
    merged, overrides_by_role = merge_acquisition_manifests(passes)
    reject_ready_regressions(passes, {row["analysis_id"]: row for row in merged})

    manifest_ids = [row["analysis_id"] for row in merged]
    pool = set(candidates.analysis_ids)
    missing_from_manifest = sorted(pool - set(manifest_ids))
    outside_pool = sorted(set(manifest_ids) - pool)
    if missing_from_manifest or outside_pool:
        problems: list[str] = []
        if missing_from_manifest:
            problems.append(
                f"{len(missing_from_manifest)} candidate(s) have no acquisition row: "
                f"{', '.join(missing_from_manifest[:10])}"
            )
        if outside_pool:
            problems.append(
                f"{len(outside_pool)} acquisition row(s) are outside the "
                f"{store_key!r} candidate pool: {', '.join(outside_pool[:10])}"
            )
        raise InventoryError("; ".join(problems))

    rows = tuple(
        SourceInventoryRow(
            analysis_id=row["analysis_id"],
            publication_pmid=row["publication_pmid"],
            trait=row["trait"],
            study_design=row["study_design"],
            sample_size=row["sample_size"],
            readiness_status=row["status"],
            data_url=row["data_url"],
            yaml_url=row["yaml_url"],
            data_file=row["data_file"],
            yaml_file=row["yaml_file"],
            data_bytes=row["data_bytes"],
            yaml_bytes=row["yaml_bytes"],
            sha256=row["sha256"],
            error=row["error"],
        )
        for row in merged
    )

    invalid_ready: list[str] = []
    for row in rows:
        if row.ready:
            missing_fields: list[str] = []
            if not row.data_file.strip():
                missing_fields.append("data_file")
            if not row.data_url.strip():
                missing_fields.append("data_url")
            if not row.sha256.strip():
                missing_fields.append("sha256")
            if not row.data_bytes.strip() or row.recorded_bytes is None:
                missing_fields.append("data_bytes")
            if missing_fields:
                invalid_ready.append(f"{row.analysis_id} (missing {', '.join(missing_fields)})")

    if invalid_ready:
        raise InventoryError(
            f"{len(invalid_ready)} ready row(s) missing required field(s): {', '.join(invalid_ready[:10])}"
        )

    inputs = tuple(
        ManifestInput(
            role=manifest.role,
            path=manifest.path,
            sha256=sha256_file(manifest.path),
            bytes=manifest.path.stat().st_size,
            rows=len(rows),
            readiness_counts=_readiness_counts(rows),
            overrides=overrides_by_role[manifest.role],
        )
        for manifest, (_, rows) in zip(manifests, passes)
    )

    return InventorySnapshot(
        snapshot_id=snapshot_id,
        source_collection_id=source_collection_id,
        store_key=store_key,
        ancestry_group=ancestry_group,
        rows=rows,
        inputs=inputs,
        candidates=candidates,
        frozen_at=frozen_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def render_inventory_tsv(snapshot: InventorySnapshot) -> str:
    """Render the frozen inventory TSV, one row per discovered Analysis."""
    lines = ["\t".join(INVENTORY_COLUMNS)]
    for row in snapshot.rows:
        record = row.as_dict()
        for name in INVENTORY_COLUMNS:
            if "\t" in record[name] or "\n" in record[name]:
                raise InventoryError(
                    f"{row.analysis_id}: {name} contains a tab or newline and cannot be written "
                    "as one TSV field"
                )
        lines.append("\t".join(record[name] for name in INVENTORY_COLUMNS))
    return "\n".join(lines) + "\n"


def render_provenance_yaml(snapshot: InventorySnapshot, inventory_sha256: str) -> str:
    """Render the provenance sidecar that makes the snapshot's identity reviewable."""
    document: dict[str, Any] = {
        "snapshot_id": snapshot.snapshot_id,
        "source_collection_id": snapshot.source_collection_id,
        "store_key": snapshot.store_key,
        "ancestry_group": snapshot.ancestry_group,
        "frozen_at": snapshot.frozen_at,
        "rows": len(snapshot.rows),
        "readiness_counts": snapshot.readiness_counts,
        "ready_rows": len(snapshot.ready_rows),
        "ready_bytes": snapshot.ready_bytes,
        "study_design_counts": snapshot.study_design_counts,
        "ready_study_design_counts": snapshot.ready_study_design_counts,
        "duplicate_content_groups": [group.as_dict() for group in snapshot.duplicates],
        "inventory_tsv_sha256": inventory_sha256,
        "inputs": [entry.as_dict() for entry in snapshot.inputs],
        "candidates": {
            "path": str(snapshot.candidates.path),
            "sha256": snapshot.candidates.sha256,
            "bytes": snapshot.candidates.bytes,
            "store_key": snapshot.candidates.store_key,
            "selected_rows": len(snapshot.candidates.analysis_ids),
            "study_design_counts": dict(snapshot.candidates.study_design_counts),
        },
    }
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)


def write_snapshot(snapshot: InventorySnapshot, inventory_path: Path, provenance_path: Path) -> str:
    """Write the inventory and its sidecar atomically; return the inventory checksum."""
    text = render_inventory_tsv(snapshot)
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(inventory_path, text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    _atomic_write(provenance_path, render_provenance_yaml(snapshot, digest))
    return digest


def read_inventory(path: Path) -> tuple[SourceInventoryRow, ...]:
    """Read a frozen Source Inventory TSV, failing on duplicates or unknown status."""
    raw = _read_tsv(path, INVENTORY_COLUMNS, "source inventory")
    duplicates = _duplicate_ids(raw, "analysis_id")
    if duplicates:
        raise InventoryError(
            f"source inventory {path} has duplicate analysis_id: {', '.join(duplicates)}"
        )
    unknown = sorted({row["readiness_status"] for row in raw} - KNOWN_READINESS_STATUSES)
    if unknown:
        raise InventoryError(
            f"source inventory {path} has unknown readiness_status: {', '.join(unknown)}"
        )
    return tuple(
        SourceInventoryRow(
            **{name: row[name] for name in INVENTORY_COLUMNS}
        )
        for row in raw
    )


def duplicate_content_groups(rows: Iterable[SourceInventoryRow]) -> tuple[DuplicateContentGroup, ...]:
    """Group ready Analyses by source checksum: identical bytes under two records.

    Reported, never collapsed. Two accessions can be the same summary-statistics
    file; whether that means one member or two is a release decision for a human,
    and silently dropping one would make membership unexplainable.
    """
    by_checksum: dict[str, list[SourceInventoryRow]] = {}
    for row in rows:
        if row.ready and row.sha256:
            by_checksum.setdefault(row.sha256, []).append(row)
    groups = [
        DuplicateContentGroup(
            sha256=checksum,
            analysis_ids=tuple(sorted(row.analysis_id for row in members)),
            data_bytes=max((row.recorded_bytes or 0) for row in members) or None,
        )
        for checksum, members in by_checksum.items()
        if len(members) > 1
    ]
    groups.sort(key=lambda group: group.analysis_ids[0])
    return tuple(groups)


# ---------------------------------------------------------------------------
# Release configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceResource:
    """A declared Reference Resource, reduced to what preflight must check.

    ``auxiliary_paths`` are the resource's other required files (an
    ancestry-mixture reference is unusable without its fine-group map), so a
    resource is only "present" when every path it needs is.
    """

    resource_id: str
    kind: str
    status: str
    location: str
    location_kind: str
    version: str = ""
    auxiliary_paths: tuple[tuple[str, str], ...] = ()

    def as_dict(self, *, required: bool) -> dict[str, Any]:
        location = Path(self.location)
        present = bool(self.location) and location.exists()
        entry: dict[str, Any] = {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "status": self.status,
            "location": self.location,
            "location_kind": self.location_kind,
            "version": self.version,
            "present": present,
            "bytes": location.stat().st_size if present and location.is_file() else None,
            "required": required,
        }
        for role, value in self.auxiliary_paths:
            auxiliary = Path(value)
            entry[f"{role}_path"] = value
            entry[f"{role}_present"] = auxiliary.exists()
            entry[f"{role}_bytes"] = (
                auxiliary.stat().st_size if auxiliary.exists() and auxiliary.is_file() else None
            )
        return entry


@dataclass(frozen=True)
class MethodTier:
    """The method tier a release plans to apply to one ``study_design``."""

    study_design: str
    stored_effect_scale: str
    original_effect_scale: str
    original_sd_method: str
    sample_size_kind: str


@dataclass(frozen=True)
class ReleaseConfiguration:
    """The full-release generator config, reduced to the facts preflight asserts."""

    path: Path
    source_collection_id: str
    store_key: str
    ancestry_group: str
    inventory_snapshot_id: str
    inventory_path: Path
    inventory_provenance_path: Path
    freeze_inputs: tuple[AcquisitionPass, ...]
    candidates_path: Path
    reference_resources: Mapping[str, ReferenceResource]
    required_resource_ids: tuple[str, ...]
    ancestry_assignment_enabled: bool
    ancestry_reference_resource_id: str
    effect_scale_enabled: bool
    effect_scale_reference_resources: tuple[tuple[str, str], ...]
    method_tiers: Mapping[str, MethodTier]
    cores: int
    min_free_gb: float
    work_root: Path


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise PreflightConfigError(f"{where} is missing required key {key!r}")
    return mapping[key]


def _parse_freeze_inputs(raw: Any, path: Path) -> tuple[AcquisitionPass, ...]:
    """Parse the ordered acquisition passes, where list order is the precedence rule.

    The config lists passes earliest first, so the order in the file is the merge
    order rather than a mapping's incidental key order. A duplicate role would make
    two passes indistinguishable in the provenance, so it is rejected here.
    """
    where = f"{path}:source.inventory.freeze_inputs"
    if not isinstance(raw, list) or not raw:
        raise PreflightConfigError(
            f"{where} must be a non-empty ordered list of {{role, path}} entries, earliest pass first"
        )
    passes: list[AcquisitionPass] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise PreflightConfigError(f"{where}[{index}] is not a mapping with 'role' and 'path'")
        role = str(entry.get("role") or "").strip()
        value = str(entry.get("path") or "").strip()
        if not role or not value:
            raise PreflightConfigError(
                f"{where}[{index}] needs a non-empty 'role' and 'path'"
            )
        if role in seen:
            raise PreflightConfigError(f"{where} declares role {role!r} twice")
        seen.add(role)
        passes.append(AcquisitionPass(role=role, path=_external_path(value)))
    return tuple(passes)


def load_release_configuration(path: Path, repo_root: Path) -> ReleaseConfiguration:
    """Load the full-release generator config, failing on any missing required fact.

    Unknown top-level blocks are ignored: later Phase B stages add their own
    (acquisition, build-recipe generation) and this loader must not become a
    gate on them. Required facts are checked, never defaulted — a release whose
    method tier or Reference Resource is implicit is the silent-failure class
    this repository exists to prevent.
    """
    if not path.is_file():
        raise PreflightConfigError(f"release config not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise PreflightConfigError(f"release config {path} is not a YAML mapping")

    source = _require(document, "source", str(path))
    inventory_block = _require(source, "inventory", f"{path}:source")
    freeze_inputs = _parse_freeze_inputs(
        _require(inventory_block, "freeze_inputs", f"{path}:source.inventory"), path
    )

    resources: dict[str, ReferenceResource] = {}
    for entry in _require(document, "reference_resources", str(path)):
        resource_id = _require(entry, "resource_id", f"{path}:reference_resources")
        if resource_id in resources:
            raise PreflightConfigError(
                f"{path}:reference_resources declares resource_id {resource_id!r} twice"
            )
        resources[resource_id] = ReferenceResource(
            resource_id=resource_id,
            kind=str(_require(entry, "kind", f"{path}:reference_resources")),
            status=str(entry.get("status", "available")),
            location=str(_require(entry, "location", f"{path}:reference_resources")),
            location_kind=str(entry.get("location_kind", "")),
            version=str(entry.get("version", "")),
            auxiliary_paths=(
                (("fine_group_map", str(entry["fine_group_map"])),)
                if entry.get("fine_group_map")
                else ()
            ),
        )

    ancestry = _require(document, "ancestry_assignment", str(path))
    effect_scale = _require(document, "effect_scale_validation", str(path))
    runtime = document.get("runtime") or {}
    output = document.get("output") or {}
    defaults = _require(document, "defaults", str(path))
    by_design = _require(defaults, "by_study_design", f"{path}:defaults")

    tiers: dict[str, MethodTier] = {}
    for design, tier in by_design.items():
        tiers[str(design)] = MethodTier(
            study_design=str(design),
            stored_effect_scale=str(_require(tier, "stored_effect_scale", f"{path}:defaults")),
            original_effect_scale=str(_require(tier, "original_effect_scale", f"{path}:defaults")),
            original_sd_method=str(_require(tier, "original_sd_method", f"{path}:defaults")),
            sample_size_kind=str(_require(tier, "sample_size_kind", f"{path}:defaults")),
        )
    if not tiers:
        raise PreflightConfigError(f"{path}:defaults.by_study_design declares no method tier")

    effect_scale_resources = tuple(
        (str(_require(entry, "ancestry", f"{path}:effect_scale_validation")),
         str(_require(entry, "resource_id", f"{path}:effect_scale_validation")))
        for entry in (effect_scale.get("reference_resources") or [])
    )

    required_resource_ids: list[str] = []
    if bool(ancestry.get("enabled")):
        required_resource_ids.append(
            str(_require(ancestry, "reference_resource_id", f"{path}:ancestry_assignment"))
        )
    required_resource_ids.extend(resource_id for _ancestry, resource_id in effect_scale_resources)
    for resource_id in required_resource_ids:
        if resource_id not in resources:
            raise PreflightConfigError(
                f"{path} references Reference Resource {resource_id!r} that it does not declare "
                "in reference_resources"
            )

    return ReleaseConfiguration(
        path=Path(path),
        source_collection_id=str(_require(source, "source_collection_id", f"{path}:source")),
        store_key=str(_require(source, "store_key", f"{path}:source")),
        ancestry_group=str(_require(source, "ancestry_group", f"{path}:source")),
        inventory_snapshot_id=str(_require(inventory_block, "snapshot_id", f"{path}:source.inventory")),
        inventory_path=_repo_path(repo_root, str(_require(inventory_block, "path", f"{path}:source.inventory"))),
        inventory_provenance_path=_repo_path(
            repo_root, str(_require(inventory_block, "provenance_path", f"{path}:source.inventory"))
        ),
        freeze_inputs=freeze_inputs,
        candidates_path=_repo_path(repo_root, str(_require(source, "candidates", f"{path}:source"))),
        reference_resources=resources,
        required_resource_ids=tuple(dict.fromkeys(required_resource_ids)),
        ancestry_assignment_enabled=bool(ancestry.get("enabled")),
        ancestry_reference_resource_id=str(ancestry.get("reference_resource_id", "")),
        effect_scale_enabled=bool(effect_scale.get("enabled")),
        effect_scale_reference_resources=effect_scale_resources,
        method_tiers=tiers,
        cores=int(runtime.get("cores", 1)),
        min_free_gb=float(runtime.get("min_free_gb", 0)),
        work_root=_external_path(
            str(_require(output, "work_root", f"{path}:output"))
        ),
    )


def discover_reference_resources(repo_root: Path) -> dict[str, ReferenceResource]:
    """Every Reference Resource this repository declares, by ``resource_id``.

    Reported so a reviewer can see at a glance that a resource the release does
    *not* require may still be absent on this host — the fact that decides
    whether a reference-AF fallback is available at all.
    """
    found: dict[str, ReferenceResource] = {}
    directory = repo_root / "resources" / "reference-resources"
    if not directory.is_dir():
        return found
    for resource_yaml in sorted(directory.glob("*/resource.yaml")):
        document = yaml.safe_load(resource_yaml.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not document.get("resource_id"):
            continue
        location = document.get("location") or document.get("root") or ""
        fine_group_map = document.get("fine_group_map")
        found[str(document["resource_id"])] = ReferenceResource(
            resource_id=str(document["resource_id"]),
            kind=str(document.get("kind", "")),
            status=str(document.get("status", "")),
            location=str(location),
            location_kind=str(document.get("location_kind", "")),
            version=str(document.get("version", "")),
            auxiliary_paths=(("fine_group_map", str(fine_group_map)),) if fine_group_map else (),
        )
    return found


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightResult:
    """The machine-readable preflight report plus its blocking failures."""

    report: dict[str, Any]
    failures: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def preflight(
    *,
    inventory_path: Path,
    provenance_path: Path,
    rows: Sequence[SourceInventoryRow],
    config: ReleaseConfiguration,
    repo_root: Path,
    cores: int | None = None,
    generated_at: str | None = None,
) -> PreflightResult:
    """Prove the frozen inventory still describes reality before any row is read.

    Reads the inventory, its provenance sidecar, the declared Reference Resource
    declarations, small per-Analysis metadata, and filesystem metadata only. It
    never opens a GWAS-SSF association file and never recomputes a large
    checksum. The snapshot's own identity comes from the sidecar, which is also
    what binds the inventory TSV to the bytes that were frozen.
    """
    failures: list[str] = []
    effective_cores = config.cores if cores is None else cores
    cores_source = "config" if cores is None else "cli"

    readiness = _counts(row.readiness_status for row in rows)
    ready = [row for row in rows if row.ready]

    # --- ready records completeness -----------------------------------------
    invalid_ready: list[str] = []
    for row in ready:
        missing_fields: list[str] = []
        if not row.data_file.strip():
            missing_fields.append("data_file")
        if not row.data_url.strip():
            missing_fields.append("data_url")
        if not row.sha256.strip():
            missing_fields.append("sha256")
        if not row.data_bytes.strip() or row.recorded_bytes is None:
            missing_fields.append("data_bytes")
        if missing_fields:
            invalid_ready.append(f"{row.analysis_id} (missing {', '.join(missing_fields)})")

    if invalid_ready:
        failures.append(
            f"{len(invalid_ready)} ready Analysis row(s) are missing required field(s): {', '.join(invalid_ready[:10])}"
        )

    # --- planned method tiers ------------------------------------------------
    tier_counts: dict[str, int] = {}
    unplanned: dict[str, int] = {}
    for row in ready:
        if row.study_design in config.method_tiers:
            tier_counts[row.study_design] = tier_counts.get(row.study_design, 0) + 1
        else:
            unplanned[row.study_design] = unplanned.get(row.study_design, 0) + 1
    if unplanned:
        failures.append(
            "no declared method tier for study_design "
            + ", ".join(f"{design!r} ({count} ready Analyses)" for design, count in sorted(unplanned.items()))
        )

    # --- source files --------------------------------------------------------
    missing: list[str] = []
    size_mismatch: list[str] = []
    metadata_missing: list[str] = []
    metadata_unreadable: list[str] = []
    metadata_gate_failed: list[str] = []
    not_ready_with_present_file: list[str] = []
    for row in rows:
        if not row.ready:
            if row.data_file and Path(row.data_file).is_file():
                not_ready_with_present_file.append(row.analysis_id)
            continue
        if not row.data_file or not Path(row.data_file).is_file():
            missing.append(row.analysis_id)
            continue
        source = Path(row.data_file)
        recorded = row.recorded_bytes
        if recorded is not None and source.stat().st_size != recorded:
            size_mismatch.append(f"{row.analysis_id} ({source.stat().st_size} != {recorded})")
        if not row.yaml_file or not Path(row.yaml_file).is_file():
            metadata_missing.append(row.analysis_id)
            continue
        metadata = Path(row.yaml_file)
        try:
            document = yaml.safe_load(metadata.read_text(encoding="utf-8", errors="replace"))
        except yaml.YAMLError as exc:
            metadata_unreadable.append(f"{row.analysis_id} ({exc.__class__.__name__})")
            continue
        if not isinstance(document, dict):
            metadata_unreadable.append(f"{row.analysis_id} (not a YAML mapping)")
            continue
        assembly = str(document.get("genome_assembly", "")).strip()
        harmonised = document.get("is_harmonised")
        if assembly != REQUIRED_SOURCE_ASSEMBLY or harmonised is not True:
            metadata_gate_failed.append(
                f"{row.analysis_id} (genome_assembly={assembly or 'missing'!r}, "
                f"is_harmonised={harmonised!r})"
            )

    if missing:
        failures.append(
            f"{len(missing)} ready Analysis source file(s) are missing: {', '.join(missing[:10])}"
        )
    if size_mismatch:
        failures.append(
            f"{len(size_mismatch)} ready Analysis source file(s) changed size since the freeze: "
            f"{', '.join(size_mismatch[:10])}"
        )
    if metadata_missing:
        failures.append(
            f"{len(metadata_missing)} ready Analysis metadata file(s) are missing: "
            f"{', '.join(metadata_missing[:10])}"
        )
    if metadata_unreadable:
        failures.append(
            f"{len(metadata_unreadable)} ready Analysis metadata file(s) are unreadable: "
            f"{', '.join(metadata_unreadable[:10])}"
        )
    if metadata_gate_failed:
        failures.append(
            f"{len(metadata_gate_failed)} ready Analysis metadata file(s) no longer declare "
            f"{REQUIRED_SOURCE_ASSEMBLY} harmonised source: {', '.join(metadata_gate_failed[:10])}"
        )

    # --- Reference Resources -------------------------------------------------
    declared = discover_reference_resources(repo_root)
    declared.update({rid: res for rid, res in config.reference_resources.items()})
    resource_report = [
        res.as_dict(required=res.resource_id in config.required_resource_ids)
        for res in sorted(declared.values(), key=lambda res: res.resource_id)
    ]
    warnings: list[str] = []
    for resource_id in config.required_resource_ids:
        resource = config.reference_resources[resource_id]
        if not resource.version:
            warnings.append(
                f"required Reference Resource {resource_id!r} ({resource.kind}) has no declared version"
            )
        if not resource.location or not Path(resource.location).exists():
            failures.append(
                f"required Reference Resource {resource_id!r} ({resource.kind}) is not present at "
                f"{resource.location!r}"
            )
            continue
        for role, value in resource.auxiliary_paths:
            if not Path(value).exists():
                failures.append(
                    f"required Reference Resource {resource_id!r} is missing its {role} at {value!r}"
                )

    # --- runtime, work root, disk -------------------------------------------
    if effective_cores < 1:
        failures.append(f"requested cores must be at least 1, got {effective_cores}")
    if effective_cores > MAX_CORES:
        failures.append(
            f"requested cores {effective_cores} exceeds the {MAX_CORES}-core cap for this release "
            "(issue #150)"
        )

    work_root = config.work_root
    created = False
    writable = False
    work_root_error = ""
    try:
        if not work_root.exists():
            work_root.mkdir(parents=True, exist_ok=True)
            created = True
        if not work_root.is_dir():
            work_root_error = "exists but is not a directory"
        else:
            writable = os.access(work_root, os.W_OK | os.X_OK)
            if not writable:
                work_root_error = "exists but is not writable"
    except OSError as exc:
        work_root_error = f"cannot be created: {exc}"
    if work_root_error:
        failures.append(f"work root {work_root} is unusable: {work_root_error}")

    free_gb: float | None = None
    if work_root.is_dir():
        try:
            free_gb = round(shutil.disk_usage(work_root).free / 1_000_000_000, 1)
        except OSError as exc:
            failures.append(f"cannot measure free space under {work_root}: {exc}")
    if free_gb is not None and config.min_free_gb and free_gb < config.min_free_gb:
        failures.append(
            f"free space under {work_root} is {free_gb} GB, below the declared minimum "
            f"{config.min_free_gb} GB"
        )

    # --- inventory identity & accounting ------------------------------------
    inventory_sha256 = sha256_file(inventory_path) if inventory_path.is_file() else ""
    provenance_sha256 = ""
    inventory_snapshot_id = ""
    if not provenance_path.is_file():
        failures.append(f"inventory provenance sidecar is missing: {provenance_path}")
    else:
        provenance = yaml.safe_load(provenance_path.read_text(encoding="utf-8"))
        if not isinstance(provenance, dict):
            failures.append(f"inventory provenance sidecar is not a YAML mapping: {provenance_path}")
        else:
            inventory_snapshot_id = str(provenance.get("snapshot_id", ""))
            provenance_sha256 = str(provenance.get("inventory_tsv_sha256", ""))
            if provenance_sha256 != inventory_sha256:
                failures.append(
                    f"inventory {inventory_path} does not match the checksum recorded in "
                    f"{provenance_path}; the frozen snapshot was edited after freezing"
                )
            sidecar_rows = provenance.get("rows")
            if sidecar_rows is not None and sidecar_rows != len(rows):
                failures.append(
                    f"inventory {inventory_path} has {len(rows)} row(s), but provenance sidecar records {sidecar_rows}"
                )
            sidecar_readiness = provenance.get("readiness_counts")
            if sidecar_readiness is not None and sidecar_readiness != readiness:
                failures.append(
                    f"inventory {inventory_path} readiness counts do not match provenance sidecar: "
                    f"{readiness} != {sidecar_readiness}"
                )
            sidecar_ready_rows = provenance.get("ready_rows")
            if sidecar_ready_rows is not None and sidecar_ready_rows != len(ready):
                failures.append(
                    f"inventory {inventory_path} has {len(ready)} ready row(s), but provenance sidecar records {sidecar_ready_rows}"
                )
            sidecar_ready_bytes = provenance.get("ready_bytes")
            calc_ready_bytes = sum(
                row.recorded_bytes for row in ready if row.recorded_bytes is not None
            )
            if sidecar_ready_bytes is not None and calc_ready_bytes != sidecar_ready_bytes:
                failures.append(
                    f"inventory {inventory_path} ready bytes ({calc_ready_bytes}) do not match provenance sidecar ({sidecar_ready_bytes})"
                )
    if inventory_snapshot_id != config.inventory_snapshot_id:
        failures.append(
            f"inventory snapshot {inventory_snapshot_id or 'unknown'!r} does not match the snapshot "
            f"this release declares ({config.inventory_snapshot_id!r})"
        )

    duplicates = duplicate_content_groups(rows)
    expected_exclusions = {
        f"readiness:{status}": count
        for status, count in sorted(readiness.items())
        if status not in READY_STATUSES
    }

    report: dict[str, Any] = {
        "preflight_version": PREFLIGHT_VERSION,
        "generated_at": generated_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inventory": {
            "snapshot_id": inventory_snapshot_id,
            "path": str(inventory_path),
            "sha256": inventory_sha256,
            "provenance_path": str(provenance_path),
            "provenance_sha256": provenance_sha256,
            "rows": len(rows),
            "readiness_counts": readiness,
            "ready_rows": len(ready),
            "ready_bytes": sum(row.recorded_bytes for row in ready if row.recorded_bytes is not None),
            "study_design_counts": _counts(row.study_design for row in rows),
            "ready_study_design_counts": _counts(row.study_design for row in ready),
        },
        "selection": {
            "source_collection_id": config.source_collection_id,
            "store_key": config.store_key,
            "ancestry_group": config.ancestry_group,
            "config_path": str(config.path),
        },
        "plan": {
            "cores": effective_cores,
            "cores_source": cores_source,
            "max_cores": MAX_CORES,
            "method_tiers": {
                design: {
                    "n": tier_counts.get(design, 0),
                    "stored_effect_scale": tier.stored_effect_scale,
                    "original_effect_scale": tier.original_effect_scale,
                    "original_sd_method": tier.original_sd_method,
                    "sample_size_kind": tier.sample_size_kind,
                }
                for design, tier in sorted(config.method_tiers.items())
            },
            "planned_tier_counts": dict(sorted(tier_counts.items())),
            "expected_exclusions": expected_exclusions,
            "ancestry_assignment": {
                "enabled": config.ancestry_assignment_enabled,
                "reference_resource_id": config.ancestry_reference_resource_id,
            },
            "effect_scale_validation": {
                "enabled": config.effect_scale_enabled,
                "reference_resources": [
                    {"ancestry": ancestry, "resource_id": resource_id}
                    for ancestry, resource_id in config.effect_scale_reference_resources
                ],
            },
            "reference_af_fallback_policy": (
                "declared"
                if config.effect_scale_reference_resources
                else "none_declared"
            ),
        },
        "work_root": {
            "path": str(work_root),
            "created": created,
            "writable": writable,
            "free_gb": free_gb,
            "min_free_gb": config.min_free_gb,
        },
        "source_files": {
            "checked": len(ready),
            "missing": sorted(missing),
            "size_mismatch": sorted(size_mismatch),
            "metadata_missing": sorted(metadata_missing),
            "metadata_unreadable": sorted(metadata_unreadable),
            "metadata_gate_failed": sorted(metadata_gate_failed),
            "not_ready_with_present_file": sorted(not_ready_with_present_file),
            "invalid_ready": sorted(invalid_ready),
        },
        "duplicate_content_groups": [group.as_dict() for group in duplicates],
        "reference_resources": resource_report,
        "required_reference_resources": list(config.required_resource_ids),
        "warnings": warnings,
        "failures": failures,
    }
    return PreflightResult(
        report=report, failures=tuple(failures), warnings=tuple(warnings)
    )


def render_preflight_summary(report: Mapping[str, Any]) -> str:
    """Render the operator-facing summary of a preflight report."""
    inventory = report["inventory"]
    plan = report["plan"]
    source_files = report["source_files"]
    lines = [
        f"Source Inventory {inventory['snapshot_id']} ({inventory['path']})",
        f"  rows                 {inventory['rows']}",
        f"  readiness            "
        + ", ".join(f"{status}={count}" for status, count in inventory["readiness_counts"].items()),
        f"  ready                {inventory['ready_rows']} Analyses, "
        f"{inventory['ready_bytes'] / 1e12:.3f} TB compressed",
        f"  ready study design   "
        + ", ".join(
            f"{design}={count}" for design, count in inventory["ready_study_design_counts"].items()
        ),
    ]
    for design, tier in plan["method_tiers"].items():
        lines.append(
            f"  planned tier         {design}: {tier['stored_effect_scale']} / "
            f"{tier['original_sd_method']} ({tier['n']} Analyses)"
        )
    lines.append(f"  reference-AF fallback {plan['reference_af_fallback_policy']}")
    lines.append(
        f"  work root            {report['work_root']['path']} "
        f"({report['work_root']['free_gb']} GB free, minimum {report['work_root']['min_free_gb']} GB)"
    )
    lines.append(
        f"  cores                {plan['cores']} (from {plan['cores_source']}, cap {plan['max_cores']})"
    )
    metadata_failed_count = (
        len(source_files.get("metadata_missing", []))
        + len(source_files.get("metadata_unreadable", []))
        + len(source_files.get("metadata_gate_failed", []))
    )
    lines.append(
        f"  source files checked {source_files['checked']} "
        f"(missing {len(source_files['missing'])}, "
        f"size mismatch {len(source_files['size_mismatch'])}, "
        f"metadata failed {metadata_failed_count})"
    )
    not_ready_with_present_file = source_files.get("not_ready_with_present_file", [])
    if not_ready_with_present_file:
        lines.append(
            f"  stale snapshot       {len(not_ready_with_present_file)} non-ready row(s) have files on disk"
        )
    duplicate_groups = report["duplicate_content_groups"]
    if duplicate_groups:
        lines.append(f"  duplicate content    {len(duplicate_groups)} group(s) for review:")
        for group in duplicate_groups:
            lines.append(
                f"                       {' = '.join(group['analysis_ids'])} "
                f"({group['data_bytes']} bytes)"
            )
    if report.get("warnings"):
        lines.append("WARNINGS:")
        lines.extend(f"  - {warning}" for warning in report["warnings"])
    if report["failures"]:
        lines.append("FAILED:")
        lines.extend(f"  - {failure}" for failure in report["failures"])
    else:
        lines.append("OK: preflight passed; no association rows were read.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _repo_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _external_path(value: str) -> Path:
    return Path(value)


def _atomic_write(path: Path, text: str) -> None:
    """Write through a temporary sibling so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
