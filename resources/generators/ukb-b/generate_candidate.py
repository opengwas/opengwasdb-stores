#!/usr/bin/env python3
"""Emit the restored full ukb-b observed Release Manifest Candidate (issue #219).

Turns the frozen Source Inventory (`resources/inventories/ukb-b-restored-2026-10-11.tsv`)
plus the recorded OpenGWAS API resolution into a current-shape candidate Release
Bundle under `stores/<store-id>/`, and refuses to emit anything that does not
pass the registry's executable bundle contract.

What it owns, and what it deliberately does not:

- Membership is exactly the frozen inventory: every declared, verified Analysis
  appears once, with its source path, size and sha256. A missing file, a
  duplicate, or a resolution record outside the inventory is a hard failure.
- Analytical Metadata (effect scale, sample size, trait mapping) comes from the
  recorded `opengwasdb`/OpenGWAS API resolution output. This emitter does not
  re-run the resolver and does not re-derive statistics.
- A recorded exclusion (`exclude_from_build: true`, with its reason) is
  preserved; an Analysis with no resolution record is excluded with an explicit
  reason rather than silently defaulted.
- Ancestry is a batch-level source fallback by default (see config-candidate.yaml);
  if `--ancestry-records` is supplied (e.g. from `opengwasdb resolve-analyses`),
  its per-Analysis values are used instead and the ancestry check is marked run.
- It never builds, completes, accepts, validates or publishes a Store, and it
  never overwrites an existing bundle.

Usage:

    python3 resources/generators/ukb-b/generate_candidate.py OGS-00017
    python3 resources/generators/ukb-b/generate_candidate.py OGS-00017 \\
        --ancestry-records /data/opengwasdb/work/epic232/219/ancestry.tsv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
for extra in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

# The inventory module lives beside this script in a hyphenated directory that
# is not importable as a package; load it by path.
_spec = importlib.util.spec_from_file_location("ukb_inventory", Path(__file__).with_name("inventory.py"))
assert _spec and _spec.loader
ukb_inventory = importlib.util.module_from_spec(_spec)
sys.modules["ukb_inventory"] = ukb_inventory
_spec.loader.exec_module(ukb_inventory)

from ogstores import bundle as bundle_module  # noqa: E402
from ogstores import index as index_module  # noqa: E402
from ogstores.paths import is_valid_store_id  # noqa: E402

DEFAULT_CONFIG = "resources/generators/ukb-b/config-candidate.yaml"

#: Canonical bundle columns this emitter writes, in order. A superset of the
#: OpenGWASDB shared-core required set and the registry-only fields a reviewer
#: needs to locate and explain each membership decision.
ANALYSES_COLUMNS: tuple[str, ...] = (
    "analysis_index",
    "analysis_id",
    "source_analysis_id",
    "source_label",
    "analysis_label",
    "trait_ontology_label",
    "trait_ontology_id",
    "trait_ontology_mapping_method",
    "source_file",
    "source_bundle_id",
    "checksum",
    "checksum_algorithm",
    "size_bytes",
    "source_genome_build",
    "license",
    "publication_doi",
    "publication_pmid",
    "consortium",
    "first_author",
    "source_ancestry_label",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "original_effect_scale",
    "original_sd",
    "original_sd_method",
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "n_cases",
    "n_controls",
    "analysis_group_id",
    "inclusion_reason",
    "exclude_from_build",
)

RESOLUTION_FIELDS: tuple[str, ...] = tuple(
    name for name in ANALYSES_COLUMNS if name not in {"analysis_index", "checksum", "checksum_algorithm", "size_bytes"}
)

ANCESTRY_RECORD_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "assigned_ancestry",
    "ancestry_assignment_method",
)


class CandidateError(ValueError):
    """The inputs cannot produce a schema-valid candidate Release Bundle."""


@dataclass(frozen=True)
class CandidateConfig:
    path: Path
    document: dict


# ---------------------------------------------------------------------------
# Small I/O helpers
# ---------------------------------------------------------------------------


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise CandidateError(f"TSV not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        rows = [{k: (v or "").strip() for k, v in row.items()} for row in reader]
    if not rows:
        raise CandidateError(f"TSV has no rows: {path}")
    return rows


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    return ukb_inventory.sha256_file(path)


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


# ---------------------------------------------------------------------------
# Loading inputs
# ---------------------------------------------------------------------------


def load_config(path: Path) -> CandidateConfig:
    if not path.is_file():
        raise CandidateError(f"candidate config not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise CandidateError(f"candidate config {path} is not a YAML mapping")
    return CandidateConfig(path=path, document=document)


def _index_by_id(rows: Sequence[Mapping[str, str]], what: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    for row in rows:
        analysis_id = row.get("analysis_id", "")
        if not analysis_id:
            raise CandidateError(f"{what} has a blank analysis_id")
        if analysis_id in indexed:
            duplicates.append(analysis_id)
        indexed[analysis_id] = dict(row)
    if duplicates:
        raise CandidateError(f"{what} has duplicate analysis_id: {', '.join(sorted(set(duplicates)))}")
    return indexed


def load_ancestry_records(path: Path) -> dict[str, dict[str, str]]:
    rows = _read_tsv(path)
    missing = [name for name in ANCESTRY_RECORD_COLUMNS if name not in rows[0]]
    if missing:
        raise CandidateError(f"ancestry records {path} is missing column(s): {', '.join(missing)}")
    return _index_by_id(rows, f"ancestry records {path}")


# ---------------------------------------------------------------------------
# Building the membership table
# ---------------------------------------------------------------------------


def build_analyses_rows(
    *,
    inventory: Sequence[Mapping[str, str]],
    resolution: Mapping[str, Mapping[str, str]],
    declared: Mapping[str, Mapping[str, str]],
    config: Mapping,
    ancestry_records: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Join the frozen inventory with the recorded resolution into bundle rows.

    Returns ``(rows, warnings)``. The row set is exactly the inventory, in
    inventory order. Every row is either buildable or explicitly excluded with
    a reason.
    """
    defaults = config.get("defaults") or {}
    warnings: list[str] = []
    inventory_ids = [row["analysis_id"] for row in inventory]

    outside = sorted(set(resolution) - set(inventory_ids))
    if outside:
        raise CandidateError(
            f"{len(outside)} resolution record(s) are outside the frozen inventory: {', '.join(outside[:10])}"
        )

    rows: list[dict[str, str]] = []
    for position, inv in enumerate(inventory):
        analysis_id = inv["analysis_id"]
        resolved = resolution.get(analysis_id)
        declared_row = declared.get(analysis_id, {})
        row: dict[str, str] = {name: "" for name in ANALYSES_COLUMNS}
        row["analysis_index"] = str(position)
        row["inclusion_reason"] = declared_row.get("inclusion_reason", "")

        exclusion_reason = ""
        if resolved is None:
            exclusion_reason = "no recorded resolution record for this Analysis"
        else:
            for field in RESOLUTION_FIELDS:
                if field in resolved and resolved[field] != "":
                    row[field] = resolved[field]
            if resolved.get("exclude_from_build", "").lower() == "true":
                exclusion_reason = resolved.get("inclusion_reason", "") or "recorded resolver exclusion"

        # Identity is inventory-authoritative and is applied AFTER the resolution
        # fields, so a recorded resolver path on another host can never leak into
        # the bundle: the frozen inventory's path, size and checksum always win.
        row["analysis_id"] = analysis_id
        row["source_analysis_id"] = analysis_id
        row["source_file"] = inv["source_file"]
        row["checksum"] = inv["sha256"]
        row["checksum_algorithm"] = "sha256"
        row["size_bytes"] = inv["data_bytes"]
        row["source_genome_build"] = (
            inv.get("source_genome_build") or declared_row.get("source_genome_build") or defaults.get("source_genome_build", "")
        )
        if not row["source_label"]:
            row["source_label"] = inv.get("source_label") or declared_row.get("source_label", "")

        if not row["source_label"]:
            row["source_label"] = analysis_id
        if not row["analysis_label"]:
            row["analysis_label"] = row["source_label"]
        if not row["trait_ontology_mapping_method"]:
            row["trait_ontology_mapping_method"] = "unmapped"

        # Attribution defaults fill only genuinely blank cells; a recorded value wins.
        for field in ("license", "consortium", "first_author", "analysis_group_id"):
            if not row[field]:
                row[field] = str(defaults.get(field, ""))
        if not row["source_genome_build"]:
            row["source_genome_build"] = str(defaults.get("source_genome_build", ""))

        # Ancestry: per-Analysis resolver output when supplied, else the batch
        # source fallback declared in config. This is deliberately not
        # `source_trusted_no_af` (the ukb-b VCFs carry FORMAT AF) and not
        # `af_assigned` (the resolver has not run here).
        record = (ancestry_records or {}).get(analysis_id)
        if record is not None:
            row["assigned_ancestry"] = record.get("assigned_ancestry", "")
            row["ancestry_assignment_method"] = record.get("ancestry_assignment_method", "")
            if not row["source_ancestry_label"]:
                row["source_ancestry_label"] = str(defaults.get("source_ancestry_label", ""))
        else:
            row["source_ancestry_label"] = str(defaults.get("source_ancestry_label", ""))
            row["assigned_ancestry"] = str(defaults.get("assigned_ancestry", ""))
            row["ancestry_assignment_method"] = str(defaults.get("ancestry_assignment_method", ""))

        if exclusion_reason:
            row["exclude_from_build"] = "true"
            base = row["inclusion_reason"]
            row["inclusion_reason"] = f"{base}; excluded_reason: {exclusion_reason}" if base else f"excluded_reason: {exclusion_reason}"
            warnings.append(f"{analysis_id} excluded_from_build: {exclusion_reason}")

        rows.append(row)

    if [row["analysis_id"] for row in rows] != inventory_ids:
        raise CandidateError("emitted membership does not exactly match the frozen inventory")
    return rows, warnings


def validate_membership(
    rows: Sequence[Mapping[str, str]],
    inventory: Sequence[Mapping[str, str]],
) -> None:
    """Fail loudly on any membership or identity defect before rendering."""
    inventory_ids = [row["analysis_id"] for row in inventory]
    emitted = [row["analysis_id"] for row in rows]
    if len(emitted) != len(set(emitted)):
        raise CandidateError("emitted analyses.tsv contains duplicate analysis_id")
    if set(emitted) != set(inventory_ids):
        missing = sorted(set(inventory_ids) - set(emitted))
        extra = sorted(set(emitted) - set(inventory_ids))
        raise CandidateError(f"membership mismatch: missing={missing[:10]} extra={extra[:10]}")
    digest = {row["analysis_id"]: row["sha256"] for row in inventory}
    paths = {row["analysis_id"]: row["source_file"] for row in inventory}
    sizes = {row["analysis_id"]: row["data_bytes"] for row in inventory}
    for row in rows:
        if row["checksum"] != digest[row["analysis_id"]]:
            raise CandidateError(f"{row['analysis_id']}: checksum does not match the frozen inventory")
        if row["source_file"] != paths[row["analysis_id"]]:
            raise CandidateError(
                f"{row['analysis_id']}: source_file {row['source_file']!r} is not the frozen inventory path "
                f"{paths[row['analysis_id']]!r}"
            )
        if row["size_bytes"] != sizes[row["analysis_id"]]:
            raise CandidateError(
                f"{row['analysis_id']}: size_bytes {row['size_bytes']!r} is not the frozen inventory size "
                f"{sizes[row['analysis_id']]!r}"
            )
        if len(row["checksum"]) != 64:
            raise CandidateError(f"{row['analysis_id']}: checksum is not a sha256 digest")
        if not row["size_bytes"].isdigit():
            raise CandidateError(f"{row['analysis_id']}: size_bytes is not an integer")
        if row["exclude_from_build"] not in {"", "true"}:
            raise CandidateError(f"{row['analysis_id']}: exclude_from_build must be empty or 'true'")


# ---------------------------------------------------------------------------
# Rendering the bundle
# ---------------------------------------------------------------------------


def render_analyses_tsv(rows: Sequence[Mapping[str, str]]) -> str:
    lines = ["\t".join(ANALYSES_COLUMNS)]
    for row in rows:
        for name in ANALYSES_COLUMNS:
            value = row.get(name, "")
            if "\t" in value or "\n" in value:
                raise CandidateError(f"{row['analysis_id']}: {name} contains a tab or newline")
        lines.append("\t".join(row.get(name, "") for name in ANALYSES_COLUMNS))
    return "\n".join(lines) + "\n"


def render_release_yaml(
    *,
    store_id: str,
    config: Mapping,
    inventory_path: Path,
    inventory_sha256: str,
    resolution_path: Path,
    resolution_sha256: str,
    n_rows: int,
    n_buildable: int,
    command: str,
    generator_version: str,
    created_at: str,
) -> dict:
    return {
        "store_id": store_id,
        "label": str(config.get("label", "ukb-b-full-observed-restored")),
        "access_posture": str(config.get("access_posture", "public")),
        "status": "candidate",
        "derived_from": None,
        "created_at": created_at,
        "accepted_at": None,
        "source_snapshot_id": str(config.get("source_snapshot_id", "")),
        "source_snapshot": {
            "source_collection_id": str(config.get("source_collection_id", "")),
            "inventory_path": str(inventory_path),
            "inventory_sha256": inventory_sha256,
            "resolution_path": str(resolution_path),
            "resolution_sha256": resolution_sha256,
            "resolution_snapshot_id": str(config.get("resolution_snapshot_id", "")),
            "n_analyses": n_rows,
            "n_buildable": n_buildable,
        },
        "generator": {
            "name": "resources/generators/ukb-b/generate_candidate.py",
            "version": generator_version,
            "commands": [command],
        },
        "description": str(config.get("description", "")).strip(),
        "notes": str(config.get("notes", "")).strip(),
    }


def render_build_yaml(store_id: str, config: Mapping) -> dict:
    build: dict = {
        "store_id": store_id,
        "layout": "dense",
        "completion_state": "observed_only",
        "build": {
            "command": "build-dense-vcf",
            "options": dict(config.get("build_options") or {}),
        },
        "post": dict(config.get("post") or {"top_hits": True, "overview": True}),
    }
    indexed = config.get("indexed_subsets")
    if indexed:
        build["indexed_subsets"] = indexed
    return build


def render_validation_yaml(
    *,
    n_rows: int,
    n_buildable: int,
    warnings: Sequence[str],
    ancestry_checked: bool,
    generated_at: str,
) -> dict:
    status = "passed_with_warnings" if warnings else "passed"
    return {
        "status": status,
        "validated_at": generated_at,
        "validator": {"name": "resources/generators/ukb-b/generate_candidate.py", "version": None},
        "checks": {
            "schema": "passed",
            "files": "passed",
            "reader_smoke_test": "not_run",
            "ancestry": "passed" if ancestry_checked else "not_run",
            "effect_scale": "not_run",
            "sd_estimation": "not_run",
        },
        "reports": {},
        "warnings": list(warnings),
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Validation and publish
# ---------------------------------------------------------------------------


def _validate_shared_schema(staged: Path, rows: Sequence[Mapping[str, str]]) -> None:
    """Run OpenGWASDB's shared `analyses.tsv` schema over the buildable rows."""
    from opengwasdb.model.analyses import read_analyses, validate_analyses

    buildable = [row for row in rows if row["exclude_from_build"] != "true"]
    with tempfile.TemporaryDirectory() as tmp:
        subset = Path(tmp) / "analyses.tsv"
        subset.write_text(render_analyses_tsv(buildable), encoding="utf-8")
        errors = validate_analyses(read_analyses(subset))
    if errors:
        raise CandidateError(
            "buildable analyses.tsv failed the shared OpenGWASDB schema:\n"
            + "\n".join(f" - {error}" for error in errors)
        )


def _check_staged_bundle(staged: Path) -> None:
    registry_root = staged.parent
    loaded = bundle_module.load(staged.name, registry_root=registry_root)
    result = bundle_module.check(loaded, registry_root=registry_root)
    if result:
        raise CandidateError("staged bundle failed bundle.check():\n" + "\n".join(f" - {e}" for e in result))


def emit(
    *,
    store_id: str,
    out_dir: Path,
    release: Mapping,
    build: Mapping,
    analyses_rows: Sequence[Mapping[str, str]],
    validation: Mapping,
) -> Path:
    """Stage, validate and atomically publish the candidate bundle."""
    if out_dir.exists():
        raise CandidateError(f"refusing to overwrite an existing bundle: {out_dir}")
    # bundle.load() derives the store_id from the directory name, so stage inside
    # a throwaway registry root that contains a correctly named directory.
    staging_root = out_dir.parent / f".{store_id}.staging"
    staging = staging_root / store_id
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging.mkdir(parents=True)
    try:
        _atomic_write(staging / "release.yaml", yaml.safe_dump(dict(release), sort_keys=False, allow_unicode=True))
        _atomic_write(staging / "build.yaml", yaml.safe_dump(dict(build), sort_keys=False, allow_unicode=True))
        _atomic_write(staging / "analyses.tsv", render_analyses_tsv(analyses_rows))
        _atomic_write(staging / "validation.yaml", yaml.safe_dump(dict(validation), sort_keys=False, allow_unicode=True))
        _check_staged_bundle(staging)
        index_module.write_bundle_summary(bundle_module.load(store_id, registry_root=staging_root))
        # Publish last: a crash before this leaves nothing registered.
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, out_dir)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
    return out_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id", help="candidate Store Release id, e.g. OGS-00017")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--inventory", help="override config source_snapshot inventory path")
    parser.add_argument("--resolution", help="override config resolution table")
    parser.add_argument("--declared", help="override config declared selection")
    parser.add_argument("--ancestry-records", help="per-Analysis assigned ancestry from opengwasdb resolve-analyses")
    parser.add_argument("--registry-root", default="stores")
    parser.add_argument("--out-dir", help="write the bundle here instead of <registry-root>/<store_id>")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--created-at")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root)
    if not is_valid_store_id(args.store_id):
        raise SystemExit(f"invalid Store Release id: {args.store_id!r}")

    config = load_config(_resolve(repo_root, args.config))
    document = config.document
    config_relative = Path(args.config)

    inventory_path = Path(args.inventory) if args.inventory else _resolve(repo_root, str(document["inventory"]))
    resolution_path = Path(args.resolution) if args.resolution else _resolve(repo_root, str(document["resolution"]))
    declared_path = Path(args.declared) if args.declared else _resolve(repo_root, str(document["declared"]))

    inventory = ukb_inventory.read_inventory(inventory_path)
    resolution = _index_by_id(_read_tsv(resolution_path), f"resolution table {resolution_path}")
    declared = _index_by_id(_read_tsv(declared_path), f"declared selection {declared_path}")
    ancestry_records = load_ancestry_records(Path(args.ancestry_records)) if args.ancestry_records else None

    rows, warnings = build_analyses_rows(
        inventory=inventory,
        resolution=resolution,
        declared=declared,
        config=document,
        ancestry_records=ancestry_records,
    )
    validate_membership(rows, inventory)

    n_buildable = sum(1 for row in rows if row["exclude_from_build"] != "true")
    created_at = args.created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    command = "python3 resources/generators/ukb-b/generate_candidate.py " + " ".join(sys.argv[1:])
    generator_version = "sha256:" + hashlib.sha256((REPO_ROOT / "resources/generators/ukb-b/generate_candidate.py").read_bytes()).hexdigest()

    release = render_release_yaml(
        store_id=args.store_id,
        config=document,
        inventory_path=inventory_path.resolve(),
        inventory_sha256=sha256_file(inventory_path),
        resolution_path=resolution_path.resolve(),
        resolution_sha256=sha256_file(resolution_path),
        n_rows=len(rows),
        n_buildable=n_buildable,
        command=command,
        generator_version=generator_version,
        created_at=created_at,
    )
    build = render_build_yaml(args.store_id, document)
    if not ancestry_records:
        warnings = list(warnings) + [
            "ancestry is a batch source fallback (source_fallback), not an AF-based assignment; "
            "run `opengwasdb resolve-analyses` before acceptance/build (#222)"
        ]
    validation = render_validation_yaml(
        n_rows=len(rows),
        n_buildable=n_buildable,
        warnings=warnings,
        ancestry_checked=bool(ancestry_records),
        generated_at=created_at,
    )

    out_dir = Path(args.out_dir) if args.out_dir else _resolve(repo_root, args.registry_root) / args.store_id
    _validate_shared_schema(out_dir, rows)
    published = emit(
        store_id=args.store_id,
        out_dir=out_dir,
        release=release,
        build=build,
        analyses_rows=rows,
        validation=validation,
    )
    print(
        f"Emitted candidate {args.store_id}: {len(rows)} Analyses "
        f"({n_buildable} buildable, {len(rows) - n_buildable} excluded) to {published}"
    )
    if warnings:
        for warning in warnings:
            print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
