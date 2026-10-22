"""The frozen Source Inventory: its rows, readiness vocabulary and preflight (issue #151).

Manifest Generation selects Analyses out of a **Source Inventory**: the frozen
record of which intended Analyses acquisition actually produced usable files
for. The shared freeze writes it (``manifest_generation.freeze``, #283); this
module owns what every inventory shares:

- **the inventory's rows and byte format** -- one row per selected Analysis,
  read and written here, with the counts its provenance records;
- **the readiness vocabulary** -- which acquisition outcomes mean "this Analysis
  has a verified, readable source file" and which mean "not this release's
  member", kept distinct so an unavailable input stays a Source Inventory fact
  and never looks like a selected member;
- **the GWAS Catalog's pass overlay** -- every EBI download pass writes a
  status manifest, overlaid earliest first, a later pass winning for every
  ``analysis_id`` it covers but never regressing a ready one; the GWAS Catalog
  Acquisition Manifest writer applies it
  (``providers.gwas_catalog.write_acquisition_manifest``);
- **preflight** -- the cheap proof, before any association row is read, that the
  frozen snapshot still describes the mirror on disk and belongs to the release
  config's Source Collection and Selection, that its declared Reference
  Resources exist, and that the method tiers the release plans to apply are
  declared rather than guessed.

What it deliberately does **not** do: read a full GWAS-SSF association body,
recompute a source checksum, compute ancestry or effect scale, or decide which
duplicate-content accession to build. Preflight compares recorded *sizes*, not
checksums, because checksumming 1.8 TB is not a preflight -- that belongs to the
resolution stage. Duplicate-content groups are reported for review and never
silently collapsed.

Manifest Generation's ``preflight`` is the inventory-readiness gate before
candidate selection, distinct from Store Release Build's preflight
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
from typing import Any, Callable, Iterable, Mapping, Sequence

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

#: Outcomes of *verifying* a source file against the identity recorded for it,
#: for a Source Collection whose acquisition only records identities (#289).
#:
#: ukb-b's files were copied, not downloaded, so its acquisition has no transfer
#: to report: it records each file's path, size and sha256 once, and later runs
#: re-verify the mirror against that record. Each of these is that verification
#: failing, and none of them is ready:
#:
#: - ``missing_file``: the source file is gone;
#: - ``missing_index``: the Tabix index beside it is gone;
#: - ``size_mismatch``: the file's size is not the recorded one (a truncation);
#: - ``checksum_mismatch``: its sha256 is not the recorded one;
#: - ``index_mismatch``: its index's sha256 or size is not the recorded one;
#: - ``bad_index``: the index does not open or lists no contig;
#: - ``integrity_failed``: the compressed stream does not decompress to EOF.
SOURCE_VERIFICATION_STATUSES: frozenset[str] = frozenset(
    {
        "missing_file",
        "missing_index",
        "size_mismatch",
        "checksum_mismatch",
        "index_mismatch",
        "bad_index",
        "integrity_failed",
    }
)

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
) | SOURCE_VERIFICATION_STATUSES

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


def duplicate_values(values: Iterable[str]) -> list[str]:
    """Every value that occurs more than once, sorted."""
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return sorted(repeated)


def _duplicate_ids(rows: Iterable[Mapping[str, str]], key: str) -> list[str]:
    return duplicate_values(row[key] for row in rows)


def read_acquisition_pass(path: Path) -> list[dict[str, str]]:
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


def render_inventory_rows(rows: Iterable[SourceInventoryRow]) -> str:
    """Render Source Inventory rows as the frozen TSV, in the order given.

    Shared by both freezes, so a Source Inventory has one byte format whichever
    wrote it (``manifest_generation.freeze`` is the shared one, #283).
    """
    lines = ["\t".join(INVENTORY_COLUMNS)]
    for row in rows:
        record = row.as_dict()
        for name in INVENTORY_COLUMNS:
            if "\t" in record[name] or "\n" in record[name]:
                raise InventoryError(
                    f"{row.analysis_id}: {name} contains a tab or newline and cannot be written "
                    "as one TSV field"
                )
        lines.append("\t".join(record[name] for name in INVENTORY_COLUMNS))
    return "\n".join(lines) + "\n"


def inventory_counts(rows: Sequence[SourceInventoryRow]) -> dict[str, Any]:
    """The counts every Source Inventory's provenance records and preflight re-checks.

    Shared by both freezes, so the keys preflight reads have one spelling and
    one computation whichever freeze wrote them.
    """
    ready = [row for row in rows if row.ready]
    ready_bytes = 0
    for row in ready:
        recorded = row.recorded_bytes
        if recorded is None:
            raise InventoryError(f"{row.analysis_id}: ready row has no recorded data_bytes")
        ready_bytes += recorded
    return {
        "rows": len(rows),
        "readiness_counts": counts(row.readiness_status for row in rows),
        "ready_rows": len(ready),
        "ready_bytes": ready_bytes,
        "study_design_counts": counts(row.study_design for row in rows),
        "ready_study_design_counts": counts(row.study_design for row in ready),
        "duplicate_content_groups": [group.as_dict() for group in duplicate_content_groups(rows)],
    }


def write_inventory(
    rows: Sequence[SourceInventoryRow],
    inventory_path: Path,
    provenance_path: Path,
    render_provenance: Callable[[str], str],
) -> str:
    """Write the inventory TSV, then its provenance, each atomically; return the TSV's sha256.

    ``render_provenance`` receives the TSV's sha256, which the sidecar must
    record to bind itself to the bytes that were frozen.
    """
    text = render_inventory_rows(rows)
    atomic_write(inventory_path, text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    atomic_write(provenance_path, render_provenance(digest))
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
    """A pre-#287 generator config, reduced to the facts the concordance study reads."""

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


def parse_freeze_inputs(raw: Any, path: Path) -> tuple[AcquisitionPass, ...]:
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


def parse_pass_arguments(values: Sequence[str], *, flag: str) -> tuple[AcquisitionPass, ...]:
    """Parse repeated ``ROLE=PATH`` command-line values into ordered acquisition passes.

    ``flag`` is the option the values came from, so an error names what the
    operator typed. Order is the precedence rule, exactly as in a config's
    ``freeze_inputs``; a role given twice is refused.
    """
    passes: list[AcquisitionPass] = []
    seen: set[str] = set()
    for value in values:
        role, separator, path_value = value.partition("=")
        role = role.strip()
        path_value = path_value.strip()
        if not separator or not role or not path_value:
            raise PreflightConfigError(f"{flag} expects ROLE=PATH, got {value!r}")
        if role in seen:
            raise PreflightConfigError(f"{flag} declares role {role!r} twice")
        seen.add(role)
        passes.append(AcquisitionPass(role=role, path=Path(path_value).expanduser()))
    return tuple(passes)


def load_release_configuration(path: Path, repo_root: Path) -> ReleaseConfiguration:
    """Load a pre-#287 full-release generator config, failing on any missing required fact.

    Kept only for the preregistered QC-panel concordance study
    (``qc_panel_concordance``), which is bound to
    ``resources/generators/gwas-catalog-eur-hybrid/config-full.yaml``. Manifest
    Generation reads a release config (``release_config.load_release_config``).

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
    freeze_inputs = parse_freeze_inputs(
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


#: What a frozen Source Inventory's provenance records as its writer when the
#: shared freeze wrote it (``manifest_generation.freeze``, #283).
SHARED_FREEZE: str = "manifest_generation.freeze"


def frozen_for_release_problems(
    provenance: Mapping[str, Any], provenance_path: Path, config: Any
) -> list[str]:
    """Why a frozen inventory's provenance does not belong to ``config``'s release, if it does not.

    The release reads the connectors frozen beside the inventory, so the
    inventory must be the shared freeze's, of this Source Collection, for
    exactly this Selection: a Selection edited after the freeze would
    otherwise describe a membership the inventory does not have (#287).
    """
    problems: list[str] = []
    if provenance.get("frozen_by") != SHARED_FREEZE:
        problems.append(
            f"{provenance_path} was not written by the shared freeze ({SHARED_FREEZE}; it records "
            f"frozen_by {provenance.get('frozen_by')!r}), so it has no frozen connectors; "
            "re-freeze it under a new snapshot id"
        )
        return problems
    if provenance.get("source_collection_id") != config.source_collection_id:
        problems.append(
            f"{provenance_path} froze Source Collection {provenance.get('source_collection_id')!r}, "
            f"but the release config names {config.source_collection_id!r}"
        )
    if provenance.get("selection") != config.selection.as_dict():
        problems.append(
            f"{provenance_path} was frozen for Selection {provenance.get('selection')!r}, but the "
            f"release config declares {config.selection.as_dict()!r}; a changed Selection is a "
            "new freeze under a new snapshot id"
        )
    return problems


def _listed(values: Sequence[str], limit: int = 20) -> str:
    shown = ", ".join(str(value) for value in values[:limit])
    more = len(values) - limit
    return shown + (f", ... and {more} more" if more > 0 else "")


def selection_partition_problems(
    releases: Sequence[tuple[str, str, Sequence[str]]],
    *,
    collection_ids: Iterable[str] | None = None,
) -> list[str]:
    """Why several Selections of one Source Collection do not partition it, if they do not.

    Every entry is ``(label, source_collection_id, analysis_ids)``. The retired
    dbGaP MVP partition proved its five ancestry buckets disjoint and complete
    (#290); the shared preflight proves the same thing about Selections. Two
    Selections over one collection must name the same collection, must not take
    one Analysis into two Store Releases, and -- when the collection's Source
    Metadata Table is available -- must together select every Analysis it
    intends.
    """
    problems: list[str] = []
    if not releases:
        return problems
    collection = releases[0][1]
    for label, source_collection_id, _ids in releases:
        if source_collection_id != collection:
            problems.append(
                f"Selection {label!r} names Source Collection {source_collection_id!r}, but another "
                f"Selection in this preflight names {collection!r}; one preflight is one collection"
            )
    owner: dict[str, str] = {}
    for label, _collection, ids in releases:
        for analysis_id in ids:
            if analysis_id in owner:
                problems.append(
                    f"Analysis {analysis_id} is selected by both {owner[analysis_id]!r} and "
                    f"{label!r}; Selections of one Source Collection must be disjoint"
                )
            else:
                owner[analysis_id] = label
    if collection_ids is not None:
        intended = set(collection_ids)
        missing = sorted(intended - set(owner))
        extra = sorted(set(owner) - intended)
        if missing:
            problems.append(
                f"{len(missing)} Analysis/Analyses the Source Collection intends are selected by no "
                f"Selection: {_listed(missing)}"
            )
        if extra:
            problems.append(
                f"{len(extra)} selected Analysis/Analyses are not in the Source Collection: "
                f"{_listed(extra)}"
            )
    return problems


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
    config: Any,
    repo_root: Path,
    cores: int | None = None,
    generated_at: str | None = None,
) -> PreflightResult:
    """Prove the frozen inventory still describes reality before any row is read.

    ``config`` is a loaded release config (``release_config.ReleaseConfig``).
    Reads the inventory, its provenance sidecar, the declared Reference Resource
    declarations, small per-Analysis metadata, and filesystem metadata only. It
    never opens a GWAS-SSF association file and never recomputes a large
    checksum. The snapshot's own identity comes from the sidecar, which is also
    what binds the inventory TSV to the bytes that were frozen, and the sidecar
    must record the shared freeze of this Source Collection for this Selection
    (#283, #287).

    A ready row needs the file's location, size and checksum. Its source URL is
    provenance the Acquisition Manifest may lack -- a file acquisition
    extracted or reformatted has none of its own -- so a ready row without one
    is a warning, not a failure (#287). A recorded per-Analysis metadata
    sidecar must still exist, and a ``genome_assembly`` it declares (a
    GWAS-SSF ``-meta.yaml`` does) must be the release's declared build: the
    Analysis's ``source_genome_build``, else the release config's default. A
    sidecar declaring no build, and a row acquisition recorded no sidecar for,
    are counted, not failed: the Acquisition Manifest makes the sidecar
    optional. Harmonisation is the GWAS Catalog downloader's gate
    (``metadata_rejected``), not generic preflight's (#287 review).
    """
    failures: list[str] = []
    effective_cores = config.cores if cores is None else cores
    cores_source = "config" if cores is None else "cli"

    readiness = counts(row.readiness_status for row in rows)
    ready = [row for row in rows if row.ready]

    # A Selection may declare the intended size of its membership (#290). The
    # frozen inventory is the member set, so a count that no longer matches is a
    # drifted freeze, named with both numbers.
    expected_rows = getattr(config.selection, "expect_rows", None)
    if expected_rows is not None and len(rows) != expected_rows:
        failures.append(
            f"frozen inventory {inventory_path} has {len(rows)} row(s), but the Selection "
            f"{config.selection.as_dict()} expects {expected_rows}"
        )

    # --- ready records completeness -----------------------------------------
    invalid_ready: list[str] = []
    for row in ready:
        missing_fields: list[str] = []
        if not row.data_file.strip():
            missing_fields.append("data_file")
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
    no_source_url = sorted(row.analysis_id for row in ready if not row.data_url.strip())

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
    metadata_build_undeclared: list[str] = []
    no_metadata_sidecar: list[str] = []
    frozen_table, frozen_manifest = _frozen_connectors(provenance_path, failures)
    expected_builds = _declared_builds(frozen_table)
    gate_expected: set[str] = set()
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
        if not row.yaml_file:
            no_metadata_sidecar.append(row.analysis_id)
            continue
        if not Path(row.yaml_file).is_file():
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
        # The sidecar's build against the release's declared build: the
        # Analysis's own source_genome_build, else the config's default. A
        # sidecar declaring none cannot contradict it, and is counted.
        # Harmonisation is the GWAS Catalog downloader's gate, recorded as the
        # metadata_rejected status at acquisition (#287 review).
        assembly = str(document.get("genome_assembly") or "").strip()
        if not assembly:
            metadata_build_undeclared.append(row.analysis_id)
            continue
        expected = expected_builds.get(row.analysis_id) or str(
            config.defaults.get("source_genome_build") or ""
        )
        if assembly != expected:
            gate_expected.add(expected or "(none declared)")
            metadata_gate_failed.append(f"{row.analysis_id} (genome_assembly={assembly!r})")

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
        declared_build = (
            f"the release's source_genome_build {next(iter(gate_expected))}"
            if len(gate_expected) == 1
            else "their source_genome_build"
        )
        failures.append(
            f"{len(metadata_gate_failed)} ready Analysis metadata file(s) declare a "
            f"genome_assembly that is not {declared_build}: {', '.join(metadata_gate_failed[:10])}"
        )

    # --- region-filtered acquisition ----------------------------------------
    if frozen_manifest is not None:
        failures.extend(_region_lineage_problems(provenance_path, config, frozen_table, frozen_manifest))

    # --- Reference Resources -------------------------------------------------
    declared = discover_reference_resources(repo_root)
    declared.update({rid: res for rid, res in config.reference_resources.items()})
    resource_report = [
        res.as_dict(required=res.resource_id in config.required_resource_ids)
        for res in sorted(declared.values(), key=lambda res: res.resource_id)
    ]
    warnings: list[str] = []
    if no_source_url:
        warnings.append(
            f"{len(no_source_url)} ready Analysis row(s) record no source URL; analyses.tsv "
            f"leaves source_url blank for them: {', '.join(no_source_url[:10])}"
        )
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
        # Freeze's parse, cached by content (#290 review); freeze imports this
        # module, so it is imported here.
        from manifest_generation.freeze import load_provenance

        provenance = load_provenance(provenance_path)
        if not isinstance(provenance, dict):
            failures.append(f"inventory provenance sidecar is not a YAML mapping: {provenance_path}")
        else:
            failures.extend(frozen_for_release_problems(provenance, provenance_path, config))
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
            "study_design_counts": counts(row.study_design for row in rows),
            "ready_study_design_counts": counts(row.study_design for row in ready),
        },
        "selection": {
            "source_collection_id": config.source_collection_id,
            "selection": config.selection.as_dict(),
            "expect_rows": getattr(config.selection, "expect_rows", None),
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
            "metadata_build_undeclared": sorted(metadata_build_undeclared),
            "no_metadata_sidecar": sorted(no_metadata_sidecar),
            "no_source_url": no_source_url,
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


def _frozen_connectors(provenance_path: Path, failures: list[str]) -> tuple[Any, Any]:
    """The connectors frozen beside the inventory, checked against their recorded sha256.

    Preflight proves the frozen snapshot, and the release reads these files, so
    one that is gone or changed is a named failure, never an empty table
    (#282 review). ``(None, None)`` then, and when the provenance itself is
    missing, which preflight reports on its own.
    """
    from manifest_generation.connector_tsv import FreezeError
    from manifest_generation.freeze import load_frozen_connectors

    if not provenance_path.is_file():
        return None, None
    try:
        return load_frozen_connectors(provenance_path)
    except (FreezeError, OSError) as exc:
        failures.append(f"the frozen connectors cannot be read: {exc}")
        return None, None


def _region_lineage_problems(provenance_path: Path, config: Any, table: Any, manifest: Any) -> list[str]:
    """Why the frozen acquisition is not filtered to the release's region policy (#282 review).

    ADR 0036 (clarified by #300) filters a release's files to its own policy, so
    preflight refuses, before resolution, files filtered under another policy,
    QC panel or target, a policy declared over unfiltered files, and region-filtered
    files under a config that declares none
    (``region_filtered_acquisition.region_lineage_problems``).
    """
    from manifest_generation.region_filtered_acquisition import (
        frozen_evidence,
        region_lineage_problems,
    )

    try:
        evidence = frozen_evidence(provenance_path)
    except InventoryError as exc:
        return [f"acquisition's region-filtering evidence cannot be read: {exc}"]
    return region_lineage_problems(config, table, manifest, evidence)


def _declared_builds(table: Any) -> dict[str, str]:
    """Each Analysis's ``source_genome_build`` from the frozen Source Metadata Table.

    Blank cells are absent: the release config's default applies to them.
    """
    if table is None:
        return {}
    return {
        row["source_analysis_id"]: row["source_genome_build"]
        for row in table.rows
        if row.get("source_genome_build")
    }


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


def counts(values: Iterable[str]) -> dict[str, int]:
    """How often each value occurs, sorted by value."""
    tally: dict[str, int] = {}
    for value in values:
        tally[value] = tally.get(value, 0) + 1
    return dict(sorted(tally.items()))


def _repo_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _external_path(value: str) -> Path:
    return Path(value)


def atomic_write(path: Path, text: str) -> None:
    """Write through a temporary sibling so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
