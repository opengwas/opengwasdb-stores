#!/usr/bin/env python3
"""Emit unbuilt, acceptance-pending MVP ancestry and META candidates (#221).

This is Phase B tooling: it turns the metadata-only MVP partition into candidate
Release Bundles for the single-ancestry Stores and the cross-ancestry META
Store. It is deliberately conservative.

* It emits ``status: candidate`` bundles with no ``accepted_at`` and a
  ``validation.yaml`` ``acceptance`` block whose human-acceptance field stays
  ``pending``. Nothing here accepts, registers, builds, completes or publishes a
  Store, and no existing bundle is overwritten.
* The ancestry partition is validated against the intended bound counts and
  must be disjoint; a missing or drifted membership fails loudly.
* META is emitted observed-only: no ``derived_from`` and no completion block,
  regardless of the fact that an EUR panel exists.
* A lineage-linked completed child is emitted only when its ``completion_reference``
  is ``qualified``. A ``hold`` reference (AMR today) produces a recorded plan,
  not a buildable bundle.
* Without a resolution table it runs in ``plan`` mode: it writes the validated
  partition and a per-store plan only, refusing to invent Analytical Metadata.

Usage:

    python3 resources/generators/dbgap-mvp/generate_candidate.py plan \\
        --mapping /data/opengwasdb/work/epic232/220/conversion-retry-.../mapping.tsv \\
        --out-dir /data/opengwasdb/work/epic232/221

    python3 resources/generators/dbgap-mvp/generate_candidate.py emit \\
        --mapping .../mapping.tsv --resolution /path/to/resolution.tsv
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
for _extra in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

# The partition module lives beside this script in a hyphenated directory that is
# not importable as a package; load it by path.
_spec = importlib.util.spec_from_file_location(
    "dbgap_mvp_partition", Path(__file__).with_name("partition.py")
)
assert _spec and _spec.loader
partition_module = importlib.util.module_from_spec(_spec)
sys.modules["dbgap_mvp_partition"] = partition_module
_spec.loader.exec_module(partition_module)

# Shared containment-checked, exclusive-temp writer (parent BAF pattern).
_safe_spec = importlib.util.spec_from_file_location(
    "ogstores_safe_write", REPO_ROOT / "resources" / "generators" / "lib" / "safe_write.py"
)
assert _safe_spec and _safe_spec.loader
safe_write = importlib.util.module_from_spec(_safe_spec)
sys.modules["ogstores_safe_write"] = safe_write
_safe_spec.loader.exec_module(safe_write)

from ogstores import bundle as bundle_module  # noqa: E402

DEFAULT_CONFIG = "resources/generators/dbgap-mvp/config-candidate.yaml"

#: The columns a resolution table must carry for a bundle to pass the registry
#: and shared-core Analysis contract.
REQUIRED_RESOLUTION_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "original_effect_scale",
    "original_sd_method",
    # Dataset-source binding: the real source file path, genome build and
    # source label/ID must be present on every row. The emitter never invents a
    # default source build or sample-size/SD value.
    "source_file",
    "source_genome_build",
    "source_label",
    "source_analysis_id",
)


class CandidateError(ValueError):
    """The inputs cannot produce a schema-valid candidate Release Bundle."""


# ---------------------------------------------------------------------------
# small I/O helpers
# ---------------------------------------------------------------------------


def read_tsv(path: Path | str) -> list[dict[str, str]]:
    tsv_path = Path(path)
    if not tsv_path.is_file():
        raise CandidateError(f"TSV not found: {tsv_path}")
    with tsv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fieldnames = tuple(reader.fieldnames or ())
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not fieldnames:
        raise CandidateError(f"TSV has no header: {tsv_path}")
    if not rows:
        raise CandidateError(f"TSV has no data rows: {tsv_path}")
    return rows


def sha256_file(path: Path | str) -> str:
    """Hash a regular metadata file (never an association file)."""
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_not_registry_store_root(out_dir: Path | str) -> None:
    """Refuse to emit anywhere under the registry's active ``stores/`` tree.

    A candidate passes ``bundle.check()``, and the Phase A Snakefile targets
    every valid bundle under ``stores/``; writing there would make an unbuilt,
    acceptance-pending candidate buildable. It is not allowed.
    """
    path = Path(out_dir)
    registry_root = (REPO_ROOT / "stores").resolve()
    resolved = path.resolve()
    if resolved == registry_root or registry_root in resolved.parents:
        raise CandidateError(
            f"refusing to emit under the registry Store root {registry_root}: {path}"
        )
    if path.is_symlink():
        raise CandidateError(f"refusing to emit into a symlinked output directory: {path}")
    for parent in (path, path.parent):
        if parent.is_symlink():
            raise CandidateError(
                f"refusing to emit under a symlinked output parent: {parent}"
            )


def _finite_positive(value: object, where: str) -> None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CandidateError(f"{where} is not numeric: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise CandidateError(f"{where} must be finite and positive: {value!r}")


def validate_resolution_row(
    row: Mapping[str, str],
    ancestry: str,
    assigned_ancestry: str | None,
    manifest_row: Mapping[str, str] | None = None,
) -> None:
    """Per-row gates before any emit.

    Non-empty required values, exact source-ID binding, the manifest's own
    data-file path, the named sidecar's genome assembly, finite positive
    sample size/SD, and ancestry match. Nothing is defaulted.
    """
    analysis_id = row.get("analysis_id", "")
    for column in REQUIRED_RESOLUTION_COLUMNS:
        # META is the cross-ancestry bucket: it has no single assigned ancestry
        # by design, but every other required value must be non-empty.
        if column == "assigned_ancestry" and ancestry == "META":
            continue
        if not (row.get(column) or "").strip():
            raise CandidateError(
                f"resolution row {analysis_id!r} has a blank required column {column!r}"
            )
    declared = (row.get("assigned_ancestry") or "").strip()
    if ancestry != "META" and declared != (assigned_ancestry or ""):
        raise CandidateError(
            f"resolution row {analysis_id!r} assigned_ancestry {declared!r} does "
            f"not match the {ancestry} bucket"
        )
    if (row.get("source_analysis_id") or "").strip() != analysis_id:
        raise CandidateError(
            f"resolution row {analysis_id!r} source_analysis_id "
            f"{(row.get('source_analysis_id') or '')!r} != analysis_id"
        )
    if manifest_row is not None:
        data_file = (manifest_row.get("data_file") or "").strip()
        if (row.get("source_file") or "").strip() != data_file:
            raise CandidateError(
                f"resolution row {analysis_id!r} source_file "
                f"{(row.get('source_file') or '')!r} != manifest data_file {data_file!r}"
            )
        yaml_file = (manifest_row.get("yaml_file") or "").strip()
        if yaml_file:
            sidecar = yaml.safe_load(Path(yaml_file).read_text(encoding="utf-8")) or {}
            sidecar_assembly = str(sidecar.get("genome_assembly") or "").strip()
            if sidecar_assembly and (row.get("source_genome_build") or "").strip() != sidecar_assembly:
                raise CandidateError(
                    f"resolution row {analysis_id!r} source_genome_build "
                    f"{(row.get('source_genome_build') or '')!r} != sidecar genome_assembly "
                    f"{sidecar_assembly!r}"
                )
    _finite_positive(row.get("sample_size"), f"resolution row {analysis_id!r} sample_size")
    for column in ("original_sd", "phenotype_sd"):
        if column in row and (row.get(column) or "").strip():
            _finite_positive(row[column], f"resolution row {analysis_id!r} {column}")


def read_planned_manifests(config: Mapping) -> dict[str, int]:
    """Count the planned acquisition-manifest rows from the metadata root.

    Reads the four regular store manifests only (membership/file-path metadata);
    it never opens an association file. Returns ``{filename: rows}``.
    """
    metadata = config.get("metadata") or {}
    root = metadata.get("root")
    if not root:
        raise CandidateError("config has no metadata.root to read planned manifests from")
    manifest_dir = Path(root) / "manifests"
    counts: dict[str, int] = {}
    for store in config["stores"].values():
        filename = store.get("planned_manifest")
        if not filename or filename in counts:
            continue
        path = manifest_dir / filename
        if not path.is_file():
            raise CandidateError(f"planned manifest not found: {path}")
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle, delimiter="\t")
            next(reader, None)
            counts[filename] = sum(1 for _ in reader)
    return counts


def validate_planned_manifests(
    config: Mapping, partition: Mapping[str, Sequence[str]], counts: Mapping[str, int]
) -> None:
    """Cross-check manifest membership against the mapping partition.

    EUR and META share the combined legacy European manifest; the individual
    buckets each have their own. A manifest whose row count disagrees with the
    intended membership fails loudly.
    """
    shared: dict[str, list[str]] = {}
    for ancestry, store in config["stores"].items():
        filename = store.get("planned_manifest")
        if filename:
            shared.setdefault(filename, []).append(ancestry)
    for filename, buckets in shared.items():
        if filename not in counts:
            continue
        expected = sum(len(partition[bucket]) for bucket in buckets)
        if counts[filename] != expected:
            raise CandidateError(
                f"planned manifest {filename} has {counts[filename]} rows; "
                f"expected {expected} for {buckets}"
            )


def _metadata_root(config: Mapping) -> Path:
    root = (config.get("metadata") or {}).get("root")
    if not root:
        raise CandidateError("config has no metadata.root")
    return Path(root)


def _resolve_metadata_path(config: Mapping, value: str | None) -> Path | None:
    """Resolve a named metadata input to an existing regular file, if possible.

    Relative names are tried against this checkout and the primary checkout (the
    gitignored candidate table lives only in the primary). Returns None rather
    than guessing when the file is absent.
    """
    if not value:
        return None
    candidate = Path(value)
    bases = [Path("/")]
    if not candidate.is_absolute():
        bases = [REPO_ROOT, Path("/home/gh13047/repo/opengwasdb-stores")]
    for base in bases:
        resolved = candidate if candidate.is_absolute() else base / candidate
        if resolved.is_file():
            return resolved
    return None


def metadata_hashes(config: Mapping, mapping_path: Path | str | None = None) -> dict:
    """Actual hashes of the regular metadata inputs (never an association file)."""
    metadata = config.get("metadata") or {}
    root = _metadata_root(config)
    receipt_path = root / "parent-run-summary.json"
    result: dict = {
        "metadata_receipt": str(receipt_path),
        "metadata_receipt_sha256": sha256_file(receipt_path),
    }
    candidates_path = _resolve_metadata_path(config, metadata.get("candidates_table"))
    result["candidates_table"] = metadata.get("candidates_table")
    result["candidates_table_path"] = str(candidates_path) if candidates_path else None
    result["candidates_sha256"] = sha256_file(candidates_path) if candidates_path else None
    if mapping_path is not None:
        result["mapping_table"] = str(mapping_path)
        result["mapping_sha256"] = sha256_file(mapping_path)
    manifests = read_manifest_rows(config)
    result["planned_manifests"] = {
        filename: sha256_file(root / "manifests" / filename) for filename in manifests
    }
    return result


def verify_metadata_snapshot(config: Mapping) -> dict:
    """Verify the configured snapshot against the actual metadata receipt.

    The config is not trusted: its code sha, metadata content sha and membership
    must match the parent receipt, and the receipt must record no promotion and
    zero association opens.
    """
    metadata = config.get("metadata") or {}
    receipt_path = _metadata_root(config) / "parent-run-summary.json"
    if not receipt_path.is_file():
        raise CandidateError(f"metadata receipt not found: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("promotion") not in (False, 0, None):
        raise CandidateError("metadata receipt records a promotion; refusing to proceed")
    if int(receipt.get("association_opens", -1)) != 0:
        raise CandidateError("metadata receipt records association opens; refusing")
    for key in ("code_sha", "output_metadata_content_sha256", "membership"):
        configured = metadata.get(key)
        actual = receipt.get(key)
        if configured is not None and str(configured) != str(actual):
            raise CandidateError(
                f"config metadata.{key}={configured!r} != receipt {actual!r}"
            )
    return receipt


def read_manifest_rows(config: Mapping) -> dict[str, dict[str, dict[str, str]]]:
    """Read the four store manifests and index each by analysis_id (metadata)."""
    manifest_dir = _metadata_root(config) / "manifests"
    files: dict[str, dict[str, dict[str, str]]] = {}
    for store in config["stores"].values():
        filename = store.get("planned_manifest")
        if not filename or filename in files:
            continue
        path = manifest_dir / filename
        if not path.is_file():
            raise CandidateError(f"planned manifest not found: {path}")
        indexed: dict[str, dict[str, str]] = {}
        for row in read_tsv(path):
            analysis_id = (row.get("analysis_id") or "").strip()
            if not analysis_id:
                raise CandidateError(f"manifest {filename} has a blank analysis_id")
            if analysis_id in indexed:
                raise CandidateError(
                    f"manifest {filename} has duplicate analysis_id {analysis_id!r}"
                )
            indexed[analysis_id] = row
        files[filename] = indexed
    return files


def validate_manifest_membership(
    config: Mapping,
    partition: Mapping[str, Sequence[str]],
    manifests: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> None:
    """Exact manifest ID sets, disjoint, and matching the partition.

    A same-count set with a swapped or missing ID must fail; counts alone are
    not membership proof. Each row also needs an ``ok`` status, a 64-hex sha256
    and a non-empty data file and genome assembly.
    """
    expected: dict[str, set[str]] = {}
    for ancestry, store in config["stores"].items():
        filename = store.get("planned_manifest")
        if filename:
            expected.setdefault(filename, set()).update(partition[ancestry])
    union: set[str] = set()
    for filename, rows in manifests.items():
        ids = set(rows)
        if filename in expected and ids != expected[filename]:
            missing = expected[filename] - ids
            extra = ids - expected[filename]
            raise CandidateError(
                f"manifest {filename} ID set does not match the partition "
                f"(missing {len(missing)}, extra {len(extra)})"
            )
        overlap = union & ids
        if overlap:
            raise CandidateError(
                f"manifests overlap on {len(overlap)} analysis_id(s)"
            )
        union |= ids
        for analysis_id, row in rows.items():
            if (row.get("status") or "").strip() != "ok":
                raise CandidateError(
                    f"manifest row {analysis_id!r} status is not 'ok'"
                )
            digest = (row.get("sha256") or "").strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise CandidateError(
                    f"manifest row {analysis_id!r} has an invalid sha256 digest"
                )
            if not (row.get("data_file") or "").strip():
                raise CandidateError(f"manifest row {analysis_id!r} has no data_file")
            if not (row.get("genome_assembly") or "").strip():
                raise CandidateError(
                    f"manifest row {analysis_id!r} has no genome_assembly"
                )
    all_intended = {
        analysis_id for ancestry in partition for analysis_id in partition[ancestry]
    }
    if union != all_intended:
        raise CandidateError(
            f"manifest union has {len(union)} IDs; partition has {len(all_intended)}"
        )


def load_config(path: Path | str) -> dict:
    config_path = Path(path)
    if not config_path.is_file():
        raise CandidateError(f"candidate config not found: {config_path}")
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise CandidateError(f"candidate config {config_path} is not a YAML mapping")
    if not isinstance(document.get("stores"), dict):
        raise CandidateError(f"candidate config {config_path} has no 'stores' mapping")
    return document


def _index_unique(rows: Sequence[Mapping[str, str]], what: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        analysis_id = (row.get("analysis_id") or "").strip()
        if not analysis_id:
            raise CandidateError(f"{what} has a blank analysis_id")
        if analysis_id in indexed:
            raise CandidateError(f"{what} has duplicate analysis_id {analysis_id!r}")
        indexed[analysis_id] = dict(row)
    return indexed


# ---------------------------------------------------------------------------
# bundle rendering
# ---------------------------------------------------------------------------


def render_release_yaml(
    *,
    store_id: str,
    label: str,
    config: Mapping,
    created_at: str,
    derived_from: str | None,
    completion_state: str,
    notes: str,
    source_snapshot: Mapping | None = None,
) -> dict:
    release: dict = {
        "store_id": store_id,
        "label": label,
        "access_posture": config.get("access_posture", "public"),
        "status": "candidate",
        "derived_from": derived_from,
        "created_at": created_at,
        "source_snapshot_id": config["source_snapshot_id"],
        "generator": {"commands": list(config.get("generator_commands") or [])},
        "description": (
            f"MVP PMID {config.get('publication_pmid')} {completion_state} candidate "
            f"({label}). Unbuilt, acceptance-pending; no Store exists."
        ),
        "notes": notes,
    }
    if source_snapshot:
        release["source_snapshot"] = dict(source_snapshot)
    return release


def render_build_yaml(
    *,
    store_id: str,
    config: Mapping,
    ancestry: str | None,
    completion: bool,
    ld_panel: str | None,
) -> dict:
    build: dict = {
        "store_id": store_id,
        "layout": config.get("layout", "dense"),
        "completion_state": "reference_completed" if completion else "observed_only",
        "post": {"top_hits": True, "overview": True},
    }
    if completion:
        options: dict = {}
        if ancestry:
            options["ancestry"] = ancestry
        if ld_panel:
            options["ld-panel"] = ld_panel
        build["complete"] = {"command": "complete-dense", "options": options}
    else:
        build["build"] = {
            "command": "build-dense-vcf",
            "options": {
                "source-reader-capability": config.get("source_reader_capability"),
                "source-assembly": config.get("source_assembly"),
            },
        }
    return build


def render_validation_yaml(
    *,
    store_id: str,
    partition_total: int,
    completion_reference: Mapping | None = None,
) -> dict:
    """A candidate Validation Record with human acceptance explicitly pending."""
    acceptance: dict = {
        "status": "pending",
        "human_acceptance": "pending",
        "membership_accepted": False,
        "note": (
            "Phase B candidate only. Membership is not accepted; no Store was "
            "built or validated. Do not relabel this evidence as verified."
        ),
        "partition_total": partition_total,
    }
    if completion_reference is not None:
        acceptance["completion_reference"] = {
            "resource_id": completion_reference.get("resource_id"),
            "declared_status": completion_reference.get("declared_status"),
            "qualification": completion_reference.get("qualification"),
            "activation": completion_reference.get("activation"),
            "blocked": completion_reference.get("activation") != "active",
        }
    return {
        "status": "not_run",
        "validator": {"name": "resources/generators/dbgap-mvp/generate_candidate.py"},
        "acceptance": acceptance,
        "warnings": [
            "Candidate: membership, Analytical Metadata and the Source Inventory are "
            "not yet frozen or accepted.",
            "No Store build or validation has run.",
        ],
        "errors": [],
    }


def render_analyses_tsv(rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> str:
    import io

    fieldnames = ["analysis_index"] + [column for column in columns if column != "analysis_index"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for index, row in enumerate(rows):
        record = {column: row.get(column, "") for column in fieldnames}
        record["analysis_index"] = str(index)
        writer.writerow(record)
    return output.getvalue()
def _atomic_write(
    path: Path, text: str, root: Path, *, overwrite: bool = False
) -> None:
    """Write through the shared containment-checked, exclusive-temp helper."""
    try:
        safe_write.atomic_write_text(path, text, root, overwrite=overwrite)
    except safe_write.SafeWriteError as exc:
        raise CandidateError(str(exc)) from exc


def emit_bundle(
    out_dir: Path,
    store_id: str,
    release: Mapping,
    build: Mapping,
    validation: Mapping,
    analyses_text: str | None,
) -> Path:
    bundle_dir = out_dir / store_id
    if bundle_dir.is_symlink():
        raise CandidateError(f"refusing to write through a symlinked bundle path: {bundle_dir}")
    if bundle_dir.exists():
        raise CandidateError(f"refusing to overwrite existing bundle: {bundle_dir}")
    _atomic_write(bundle_dir / "release.yaml", yaml.safe_dump(dict(release), sort_keys=False), out_dir)
    _atomic_write(bundle_dir / "build.yaml", yaml.safe_dump(dict(build), sort_keys=False), out_dir)
    _atomic_write(bundle_dir / "validation.yaml", yaml.safe_dump(dict(validation), sort_keys=False), out_dir)
    if analyses_text is not None:
        _atomic_write(bundle_dir / "analyses.tsv", analyses_text, out_dir)
    return bundle_dir


def check_bundle(bundle_dir: Path, registry_root: Path) -> list[str]:
    """Run the registry's executable bundle contract and return its errors."""
    bundle = bundle_module.load(bundle_dir.name, registry_root=registry_root)
    return list(bundle_module.check(bundle, registry_root=registry_root))


# ---------------------------------------------------------------------------
# plan / emit
# ---------------------------------------------------------------------------


def build_partition(config: Mapping, mapping_path: Path | str):
    rows = partition_module.read_mapping(mapping_path)
    partition = partition_module.partition_mapping(rows)
    # Production config omits `partition_expected` and therefore validates
    # against the intended bound counts. A test fixture may supply its own
    # expected counts, but it cannot silently relax the real ones.
    expected = config.get("partition_expected") or partition_module.EXPECTED_COUNTS
    partition_module.validate_partition(partition, expected=expected)
    return rows, partition


def _reference_ld_panel(config: Mapping, ancestry: str) -> str | None:
    reference = (config["stores"][ancestry] or {}).get("completion_reference") or {}
    if reference.get("status") != "qualified":
        return None
    return reference.get("ld_panel") or None


def run_plan(
    config: Mapping,
    mapping_path: Path | str,
    out_dir: Path,
    manifest_counts: Mapping[str, int] | None = None,
) -> dict:
    _, partition = build_partition(config, mapping_path)
    manifest_counts = manifest_counts or {}
    if manifest_counts:
        validate_planned_manifests(config, partition, manifest_counts)
    verify_metadata_snapshot(config)
    manifests = read_manifest_rows(config)
    validate_manifest_membership(config, partition, manifests)
    assert_not_registry_store_root(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        out_dir / "partition.tsv",
        partition_module.render_partition_tsv(partition),
        out_dir,
        overwrite=True,
    )
    plan = {
        "publication_pmid": config.get("publication_pmid"),
        "source_snapshot_id": config.get("source_snapshot_id"),
        "metadata": config.get("metadata"),
        "source_hashes": metadata_hashes(config, mapping_path),
        "planned_manifest_counts": dict(manifest_counts),
        "total": sum(len(partition[ancestry]) for ancestry in partition_module.ANCESTRY_ORDER),
        "legacy_european_bucket": partition_module.legacy_european_count(partition),
        "stores": {},
    }
    for ancestry, store in config["stores"].items():
        planned = store.get("planned_manifest")
        plan["stores"][ancestry] = {
            "candidate_store_key": store.get("candidate_store_key"),
            "observed_store_id": store.get("observed_store_id"),
            "completed_store_id": store.get("completed_store_id"),
            "members": len(partition[ancestry]),
            "observed_only": bool(store.get("observed_only")),
            "completion_reference_status": (
                (store.get("completion_reference") or {}).get("qualification")
            ),
            "planned_manifest": planned,
            "planned_manifest_members": manifest_counts.get(planned) if planned else None,
            "resolution": "pending",
        }
    _atomic_write(
        out_dir / "candidate-plan.yaml",
        yaml.safe_dump(plan, sort_keys=False),
        out_dir,
        overwrite=True,
    )
    return plan


def _suggested_ld_panel(config: Mapping, ancestry: str) -> str | None:
    reference = (config["stores"][ancestry] or {}).get("completion_reference") or {}
    return reference.get("suggested_ld_panel") or None


def render_candidate_definitions(
    config: Mapping,
    manifest_counts: Mapping[str, int] | None = None,
    source_hashes: Mapping | None = None,
) -> dict:
    """The reviewed intended-membership/recipe definitions, tracked (not built)."""
    manifest_counts = manifest_counts or {}
    definitions: dict = {
        "status": "provisional-draft",
        "owner_ordering_approved": bool(config.get("owner_ordering_approved")),
        "publication_pmid": config.get("publication_pmid"),
        "source_snapshot_id": config.get("source_snapshot_id"),
        "intended_bound_total": config.get("intended_bound_total"),
        "metadata": config.get("metadata"),
        "source_hashes": dict(source_hashes or {}),
        "finngen": config.get("finngen"),
        "stores": {},
    }
    for ancestry, store in config["stores"].items():
        observed_id = store["observed_store_id"]
        reference = store.get("completion_reference") or {}
        planned = store.get("planned_manifest")
        observed = {
            "store_id": observed_id,
            "role": "observed",
            "label": store.get("label"),
            "status": "candidate-definition",
            "acceptance": "pending",
            "candidate_store_key": store.get("candidate_store_key"),
            "intended_members": store.get("intended_members"),
            "assigned_ancestry": store.get("assigned_ancestry"),
            "observed_only": bool(store.get("observed_only")),
            "planned_manifest": planned,
            "planned_manifest_members": manifest_counts.get(planned) if planned else None,
            "resolution": "pending",
            "actual_completion": "not_claimed",
            "recipe": {"command": "build-dense-vcf"},
            "in_stores": False,
        }
        completed_id = store.get("completed_store_id")
        completed = None
        if completed_id:
            completed = {
                "store_id": completed_id,
                "role": "completed-definition",
                "label": f"{store.get('label')}-completed",
                "derived_from": observed_id,
                "status": "pending",
                "acceptance": "pending",
                "human_acceptance": "pending",
                "blocked": reference.get("activation") != "active",
                "resolution": "pending",
                "actual_completion": "not_claimed",
                "recipe": {
                    "command": "complete-dense",
                    "ancestry": store.get("assigned_ancestry"),
                },
                "reference": {
                    "resource_id": reference.get("resource_id"),
                    "declared_status": reference.get("declared_status"),
                    "qualification": reference.get("qualification"),
                    "activation": reference.get("activation"),
                    "suggested_ld_panel": reference.get("suggested_ld_panel"),
                },
                "in_stores": False,
            }
        definitions["stores"][ancestry] = {"observed": observed, "completed": completed}
    return definitions


def write_candidate_definitions(
    config: Mapping, out_dir: Path, manifest_counts: Mapping[str, int] | None = None
) -> Path:
    assert_not_registry_store_root(out_dir)
    source_hashes = metadata_hashes(config)
    path = out_dir / "candidates.yaml"
    _atomic_write(
        path,
        yaml.safe_dump(
            render_candidate_definitions(config, manifest_counts, source_hashes),
            sort_keys=False,
        ),
        out_dir,
        overwrite=True,
    )
    return path


def run_emit(
    config: Mapping,
    mapping_path: Path | str,
    resolution_path: Path | str,
    out_dir: Path,
    created_at: str,
    check: bool = True,
    manifest_counts: Mapping[str, int] | None = None,
) -> dict:
    _, partition = build_partition(config, mapping_path)
    resolution_rows = read_tsv(resolution_path)
    resolution_by_id = _index_unique(resolution_rows, "resolution table")
    columns = [column for column in resolution_rows[0].keys() if column != "analysis_index"]
    missing = [column for column in REQUIRED_RESOLUTION_COLUMNS if column not in columns]
    if missing:
        raise CandidateError(
            f"resolution table is missing required column(s): {', '.join(missing)}"
        )

    # Fail closed: every intended member needs a resolution row, and no extra row
    # may sneak into a bundle.
    intended = {
        analysis_id
        for ancestry in partition_module.ANCESTRY_ORDER
        for analysis_id in partition[ancestry]
    }
    extra = set(resolution_by_id) - intended
    if extra:
        raise CandidateError(
            f"resolution table has {len(extra)} row(s) outside the intended membership"
        )

    verify_metadata_snapshot(config)
    manifests = read_manifest_rows(config)
    validate_manifest_membership(config, partition, manifests)
    manifest_index = {
        analysis_id: row for rows in manifests.values() for analysis_id, row in rows.items()
    }

    assert_not_registry_store_root(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    emitted: dict[str, str] = {}
    checked: list[str] = []
    manifest_counts = manifest_counts or {}
    metadata = config.get("metadata") or {}
    metadata_root = _metadata_root(config)
    resolution_sha = sha256_file(resolution_path)
    mapping_sha = sha256_file(mapping_path)
    receipt_path = metadata_root / "parent-run-summary.json"
    receipt_sha = sha256_file(receipt_path)
    manifest_sha = {
        filename: sha256_file(metadata_root / "manifests" / filename)
        for filename in manifests
    }
    candidates_path = _resolve_metadata_path(config, metadata.get("candidates_table"))
    candidates_sha = sha256_file(candidates_path) if candidates_path else None
    for ancestry, store in config["stores"].items():
        members = partition[ancestry]
        member_rows = []
        for analysis_id in members:
            if analysis_id not in resolution_by_id:
                raise CandidateError(
                    f"no resolution row for intended {ancestry} analysis {analysis_id!r}"
                )
            row = resolution_by_id[analysis_id]
            validate_resolution_row(
                row, ancestry, store.get("assigned_ancestry"), manifest_index.get(analysis_id)
            )
            member_rows.append(row)
        analyses_text = render_analyses_tsv(member_rows, columns)

        planned = store.get("planned_manifest")
        source_snapshot = {
            "metadata_root": metadata.get("root"),
            "code_sha": metadata.get("code_sha"),
            "output_metadata_content_sha256": metadata.get("output_metadata_content_sha256"),
            "membership": metadata.get("membership"),
            "association_opens": metadata.get("association_opens"),
            "acceptance": metadata.get("acceptance"),
            "candidates_table": metadata.get("candidates_table"),
            "candidates_table_path": str(candidates_path) if candidates_path else None,
            "candidates_sha256": candidates_sha,
            "mapping_table": metadata.get("mapping"),
            "mapping_sha256": mapping_sha,
            "metadata_receipt": str(receipt_path),
            "metadata_receipt_sha256": receipt_sha,
            "resolution_table": str(resolution_path),
            "resolution_sha256": resolution_sha,
            "planned_manifest": planned,
            "planned_manifest_members": manifest_counts.get(planned) if planned else None,
            "planned_manifest_sha256": manifest_sha.get(planned) if planned else None,
            "source_metadata_only": True,
        }

        observed_id = store["observed_store_id"]
        observed_only = bool(store.get("observed_only"))
        notes = (
            f"Ancestry bucket {ancestry}; {len(members)} intended Analyses. "
            f"candidate_store_key {store.get('candidate_store_key')}. "
            + (
                "Cross-ancestry META is observed-only by policy: no derived_from "
                "and no completion step. "
                if observed_only
                else ""
            )
            + "Unbuilt, acceptance-pending candidate; the human acceptance step is pending."
        )
        emit_bundle(
            out_dir,
            observed_id,
            render_release_yaml(
                store_id=observed_id,
                label=store["label"],
                config=config,
                created_at=created_at,
                derived_from=None,
                completion_state="observed-only",
                notes=notes,
                source_snapshot=source_snapshot,
            ),
            render_build_yaml(
                store_id=observed_id,
                config=config,
                ancestry=store.get("assigned_ancestry"),
                completion=False,
                ld_panel=None,
            ),
            render_validation_yaml(store_id=observed_id, partition_total=len(intended)),
            analyses_text,
        )
        emitted[ancestry] = observed_id

        completed_id = store.get("completed_store_id")
        if observed_only or not completed_id:
            continue
        reference = store.get("completion_reference") or {}
        suggested = _suggested_ld_panel(config, ancestry)
        # Always record the pending, blocked completion DEFINITION so the intended
        # lineage and recipe are reviewable. Qualification is never inferred from
        # a declared resource or a config label.
        _atomic_write(
            out_dir / f"{completed_id}.completion-plan.yaml",
            yaml.safe_dump(
                {
                    "store_id": completed_id,
                    "derived_from": observed_id,
                    "completion_state": "reference_completed",
                    "candidate_store_key": store.get("candidate_store_key"),
                    "reference": reference,
                    "status": "pending",
                    "blocked": reference.get("activation") != "active",
                    "human_acceptance": "pending",
                    "source_snapshot": source_snapshot,
                    "note": (
                        "Completion definition only. The reference is not qualified or "
                        "activated and the completed Store is not accepted, built or "
                        "registered. Do not execute or promote."
                    ),
                },
                sort_keys=False,
            ),
            out_dir,
        )
        if suggested is None:
            # No suggested panel: the definition above is the honest artifact; do
            # not fabricate a buildable recipe with a missing path.
            continue
        emit_bundle(
            out_dir,
            completed_id,
            render_release_yaml(
                store_id=completed_id,
                label=f"{store['label']}-completed",
                config=config,
                created_at=created_at,
                derived_from=observed_id,
                completion_state="reference-completed",
                notes=(
                    f"Lineage-linked completed definition for {observed_id} ({ancestry}); "
                    f"declared reference {reference.get('resource_id')}, qualification "
                    f"{reference.get('qualification')!r}, activation {reference.get('activation')!r}. "
                    "Unbuilt, acceptance-pending; not qualified or approved for execution."
                ),
                source_snapshot=source_snapshot,
            ),
            render_build_yaml(
                store_id=completed_id,
                config=config,
                ancestry=store.get("assigned_ancestry"),
                completion=True,
                ld_panel=suggested,
            ),
            render_validation_yaml(
                store_id=completed_id,
                partition_total=len(intended),
                completion_reference=reference,
            ),
            analyses_text,
        )
        emitted[f"{ancestry}-completed"] = completed_id

    if check:
        for store_id in emitted.values():
            errors = check_bundle(out_dir / store_id, out_dir)
            if errors:
                raise CandidateError(
                    f"emitted bundle {store_id} fails bundle.check(): "
                    + "; ".join(errors)
                )
            checked.append(store_id)

    _atomic_write(
        out_dir / "candidate-plan.yaml",
        yaml.safe_dump(
            {
                "publication_pmid": config.get("publication_pmid"),
                "source_snapshot_id": config.get("source_snapshot_id"),
                "emitted": emitted,
                "checked": checked,
                "acceptance": "pending",
            },
            sort_keys=False,
        ),
        out_dir,
        overwrite=True,
    )
    _atomic_write(
        out_dir / "partition.tsv",
        partition_module.render_partition_tsv(partition),
        out_dir,
        overwrite=True,
    )
    return {"emitted": emitted, "checked": checked}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "emit", "definitions"))
    parser.add_argument("--config", type=Path, default=REPO_ROOT / DEFAULT_CONFIG)
    parser.add_argument("--mapping", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--resolution", type=Path, default=None)
    parser.add_argument(
        "--created-at",
        default=None,
        help="ISO timestamp for emitted release.yaml files (defaults to now)",
    )
    parser.add_argument("--no-check", action="store_true", help="skip bundle.check()")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    created_at = args.created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest_counts: dict[str, int] = {}
    metadata = config.get("metadata") or {}
    if metadata.get("root") and Path(metadata["root"]).is_dir():
        manifest_counts = read_planned_manifests(config)

    if args.mode == "definitions":
        path = write_candidate_definitions(config, args.out_dir, manifest_counts)
        print(json.dumps({"definitions": str(path)}, indent=2))
        return 0

    if args.mapping is None:
        raise CandidateError(f"{args.mode} mode requires --mapping")
    if args.mode == "plan":
        plan = run_plan(config, args.mapping, args.out_dir, manifest_counts)
        print(json.dumps(plan, indent=2))
        return 0

    if args.resolution is None:
        raise CandidateError("emit mode requires --resolution; use plan for metadata-only")
    result = run_emit(
        config,
        args.mapping,
        args.resolution,
        args.out_dir,
        created_at=created_at,
        check=not args.no_check,
        manifest_counts=manifest_counts,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
