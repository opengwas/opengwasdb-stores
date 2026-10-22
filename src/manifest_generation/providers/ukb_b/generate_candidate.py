#!/usr/bin/env python3
"""Emit the restored full ukb-b Release Manifest Candidate (issues #219, #312).

Turns the frozen Source Inventory (`resources/inventories/ukb-b-restored-2026-10-11.tsv`)
plus the recorded OpenGWAS API resolution into a current-shape candidate Release
Bundle under `stores/<store-id>/`, and refuses to emit anything that does not
pass the registry's executable bundle contract.

One Store id gets one bundle (ADR 0037, #312). A `completion_reference` that is
`qualified` and `active` puts both phases in that bundle: the observed `build`
block, unchanged, and a `complete-dense` block against the LD panel its
Reference Resource declares, with `completion_state: reference_completed` and
no `derived_from`. Anything short of that emits the observed-only bundle
exactly as before. No second, completed id is ever minted.

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
- It never builds, completes, accepts, validates or publishes a Store. The
  shared emitter (`manifest_generation.emit`) lands the bundle, and it
  replaces only an earlier candidate (#248).

Usage:

    python3 -m manifest_generation.providers.ukb_b.generate_candidate OGS-00009
    python3 -m manifest_generation.providers.ukb_b.generate_candidate OGS-00009 \\
        --ancestry-records /data/opengwasdb/work/epic232/219/ancestry.tsv
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.emit import CandidateBundle, emit
from ogstores.paths import RETIRED_STORE_IDS, is_valid_store_id

#: The module this generator runs as, recorded in the bundle it emits.
MODULE = __spec__.name  # not __name__: that is "__main__" under python3 -m

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


#: Config keys ADR 0007 used to reserve a second, `derived_from`-linked completed
#: Store id. ADR 0037 completes a Store under its own id, so a config that still
#: declares one is refused rather than ignored: dropping it silently would emit
#: an observed-only bundle for a Store its author meant to complete (#308, #312).
RETIRED_CONFIG_KEYS: tuple[str, ...] = ("completed_store_id", "planned_completed_store_id")

#: Every key a candidate config may hold. Anything else is refused, so a
#: misspelt `completion_reference` cannot quietly emit an observed-only bundle
#: (#312). `store_family_id` is descriptive and read by nothing here.
CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "store_id",
        "store_family_id",
        "source_collection_id",
        "access_posture",
        "source_snapshot_id",
        "resolution_snapshot_id",
        "inventory",
        "resolution",
        "declared",
        "label",
        "description",
        "notes",
        "build_options",
        "complete_options",
        "post",
        "indexed_subsets",
        "defaults",
        "completion_reference",
    }
)

#: A `completion_reference`'s keys and the values each state may take, the
#: dbGaP MVP references' vocabulary (#308). An unknown key or value is refused.
COMPLETION_REFERENCE_KEYS: tuple[str, ...] = ("resource_id", "qualification", "activation")
QUALIFICATIONS: tuple[str, ...] = ("qualified", "pending", "hold", "not_applicable")
ACTIVATIONS: tuple[str, ...] = ("active", "pending", "blocked")

#: The completion command for this generator's Dense Stores.
COMPLETE_COMMAND = "complete-dense"


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: The legacy ukb-b inventory columns (#219). The frozen inventory this emitter
#: reads predates the shared freeze's column set and stays exactly as accepted;
#: #289 retires this emitter with the rest of ukb-b's bespoke path.
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


def read_inventory(path: Path) -> tuple[dict[str, str], ...]:
    """The frozen inventory's rows, refusing a missing column and a duplicate Analysis."""
    rows = _read_tsv(path)
    for column in ("analysis_id", "sha256"):
        if column not in rows[0]:
            raise CandidateError(f"inventory {path} is missing the {column} column")
    seen: dict[str, int] = {}
    for row in rows:
        seen[row["analysis_id"]] = seen.get(row["analysis_id"], 0) + 1
    duplicates = sorted(analysis_id for analysis_id, count in seen.items() if count > 1)
    if duplicates:
        raise CandidateError(f"inventory {path} has duplicate analysis_id: {', '.join(duplicates)}")
    return tuple(rows)


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _display_path(path: Path, repo_root: Path) -> str:
    """Prefer a repo-relative path so a committed bundle is not bound to one checkout."""
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Loading inputs
# ---------------------------------------------------------------------------


def load_config(path: Path) -> CandidateConfig:
    if not path.is_file():
        raise CandidateError(f"candidate config not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise CandidateError(f"candidate config {path} is not a YAML mapping")
    retired = [key for key in RETIRED_CONFIG_KEYS if key in document]
    if retired:
        raise CandidateError(
            f"candidate config {path} declares {', '.join(retired)}: a reference-completed Store "
            "is published under its own id, and its one bundle declares both phases (ADR 0037). "
            f"{', '.join(sorted(RETIRED_STORE_IDS))} are retired unused (#308, #312) and ids are "
            "never reused. Remove the key."
        )
    unknown = sorted(set(document) - CONFIG_KEYS)
    if unknown:
        hints = []
        for key in unknown:
            close = difflib.get_close_matches(key, CONFIG_KEYS, n=1)
            hints.append(f"{key!r} (did you mean {close[0]!r}?)" if close else repr(key))
        raise CandidateError(f"candidate config {path} has unknown key(s): {', '.join(hints)}")
    try:
        completion_reference(document)
        complete_options(document)
    except CandidateError as exc:
        raise CandidateError(f"candidate config {path}: {exc}") from exc
    return CandidateConfig(path=path, document=document)


def complete_options(config: Mapping) -> dict:
    """`complete-dense` flags the config sets beside the declared panel, as `build_options` does for the build.

    The panel and its ancestry are the Reference Resource's (`completion_block`),
    so a config that names either here is refused rather than let one silently
    override the other.
    """
    options = config.get("complete_options") or {}
    if not isinstance(options, Mapping):
        raise CandidateError(f"complete_options must be a mapping of complete-dense flags, not {options!r}")
    declared = sorted({"ld-panel", "ancestry"} & set(options))
    if declared:
        raise CandidateError(
            f"complete_options sets {', '.join(declared)}, which the completion_reference's "
            "Reference Resource declares; set it there"
        )
    return dict(options)


def completion_reference(config: Mapping) -> dict | None:
    """The config's `completion_reference`, checked, or ``None`` when it declares none.

    A reference that is there but malformed is refused: read as "not
    qualified", it would emit an observed-only bundle for a Store its config
    means to complete.
    """
    if "completion_reference" not in config:
        return None
    reference = config["completion_reference"]
    if not isinstance(reference, Mapping):
        raise CandidateError(
            f"completion_reference must be a mapping of {', '.join(COMPLETION_REFERENCE_KEYS)}, "
            f"not {reference!r}"
        )
    unknown = sorted(set(reference) - set(COMPLETION_REFERENCE_KEYS))
    missing = [key for key in COMPLETION_REFERENCE_KEYS if key not in reference]
    if unknown or missing:
        raise CandidateError(
            f"completion_reference has unknown key(s) {unknown} and lacks {missing}; "
            f"it declares exactly {', '.join(COMPLETION_REFERENCE_KEYS)}"
        )
    if not isinstance(reference["resource_id"], str) or not reference["resource_id"].strip():
        raise CandidateError(
            f"completion_reference resource_id must name a Reference Resource, not {reference['resource_id']!r}"
        )
    for key, allowed in (("qualification", QUALIFICATIONS), ("activation", ACTIVATIONS)):
        if reference[key] not in allowed:
            raise CandidateError(
                f"completion_reference {key} {reference[key]!r} is not one of {', '.join(allowed)}"
            )
    return {key: reference[key] for key in COMPLETION_REFERENCE_KEYS}


def reference_is_qualified_and_active(reference: Mapping) -> bool:
    """Whether an owner decision has qualified and activated a completion reference.

    A declared resource is not enough on its own: both `qualification` and
    `activation` must say so, as for the dbGaP MVP references (#308).
    """
    return reference["qualification"] == "qualified" and reference["activation"] == "active"


def completion_block(config: Mapping, repo_root: Path) -> dict | None:
    """The `complete` block a qualified, active reference declares, else ``None``.

    The panel and ancestry come from the reference's Reference Resource
    declaration, so the bundle names the panel the repository declares rather
    than a second copy of its path.
    """
    reference = completion_reference(config)
    if reference is None or not reference_is_qualified_and_active(reference):
        return None
    resource_id = reference["resource_id"]
    path = repo_root / "resources" / "reference-resources" / resource_id / "resource.yaml"
    if not path.is_file():
        raise CandidateError(f"completion_reference {resource_id!r} names no Reference Resource: {path} does not exist")
    declaration = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(declaration, Mapping) or declaration.get("kind") != "ld_panel":
        raise CandidateError(f"{path}: completion_reference {resource_id!r} is not a kind: ld_panel declaration")
    location = declaration.get("location")
    ancestry = declaration.get("ancestry")
    if not isinstance(location, str) or not location.strip() or not isinstance(ancestry, str) or not ancestry.strip():
        raise CandidateError(f"{path}: an LD panel for completion declares one 'location' and one 'ancestry'")
    # complete-dense joins one --ancestry into both the panel path
    # (<ld-panel>/<ancestry>/<chr>/) and the Analyses' ancestry filter, so the
    # directory must be named for the ancestry (#219: `ukb` could not be).
    if declaration.get("ancestry_dir", ancestry) != ancestry:
        raise CandidateError(
            f"{path}: ancestry_dir {declaration['ancestry_dir']!r} is not ancestry {ancestry!r}; "
            f"`opengwasdb {COMPLETE_COMMAND} --ancestry` names both the panel directory and the "
            "Analyses it completes, so no single value would serve"
        )
    return {
        "command": COMPLETE_COMMAND,
        "options": {"ld-panel": location, "ancestry": ancestry, **complete_options(config)},
    }


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

    if ancestry_records is not None:
        outside_ancestry = sorted(set(ancestry_records) - set(inventory_ids))
        if outside_ancestry:
            raise CandidateError(
                f"{len(outside_ancestry)} ancestry record(s) are outside the frozen inventory: "
                f"{', '.join(outside_ancestry[:10])}"
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


def ancestry_validation(
    admitted_ids: Sequence[str],
    ancestry_records: Mapping[str, Mapping[str, str]] | None,
) -> tuple[bool, list[str]]:
    """Decide whether ``checks.ancestry`` may be ``passed``, never on partial evidence.

    ``passed`` requires a matching record for **every** admitted (buildable)
    Analysis whose ``ancestry_assignment_method`` is ``af_assigned`` and whose
    ``assigned_ancestry`` is non-empty. A missing record, or a source_fallback /
    unassigned record, keeps the check ``not_run`` with an explicit warning, so a
    partial, extra or mixed-coverage record set can never render as a scientific
    pass. Per-Analysis overrides are still applied to the rows by
    `build_analyses_rows`; this only governs the release-level check verdict.
    """
    if not admitted_ids:
        return False, ["no admitted Analyses to check ancestry against; checks.ancestry=not_run"]
    if ancestry_records is None:
        return False, [
            "ancestry is the provider/source label (source_fallback), not an AF-based assignment; "
            "no AF-assigned ancestry records were supplied; checks.ancestry=not_run "
            "(run `opengwasdb assign-ancestry` before acceptance/build, #222)"
        ]
    missing = [aid for aid in admitted_ids if aid not in ancestry_records]
    non_af = [
        aid
        for aid in admitted_ids
        if aid in ancestry_records
        and (
            ancestry_records[aid].get("ancestry_assignment_method", "").strip() != "af_assigned"
            or not ancestry_records[aid].get("assigned_ancestry", "").strip()
        )
    ]
    if not missing and not non_af:
        return True, []
    return False, [
        f"ancestry is not fully AF-assigned: {len(missing)} of {len(admitted_ids)} admitted Analyses have no "
        f"ancestry record and {len(non_af)} have a non-af_assigned or unassigned record; checks.ancestry=not_run "
        "(a partial or mixed record set must not render as a scientific pass)"
    ]


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
    generator_version: str,
    created_at: str,
    ancestry_records_identity: Mapping[str, object] | None = None,
) -> dict:
    source_snapshot: dict = {
        "source_collection_id": str(config.get("source_collection_id", "")),
        "inventory_path": str(inventory_path),
        "inventory_sha256": inventory_sha256,
        "resolution_path": str(resolution_path),
        "resolution_sha256": resolution_sha256,
        "resolution_snapshot_id": str(config.get("resolution_snapshot_id", "")),
        "n_analyses": n_rows,
        "n_buildable": n_buildable,
    }
    if ancestry_records_identity is not None:
        source_snapshot["ancestry_records"] = dict(ancestry_records_identity)
    return {
        "store_id": store_id,
        "label": str(config.get("label", "ukb-b-full")),
        "access_posture": str(config.get("access_posture", "public")),
        "status": "candidate",
        "derived_from": None,
        "created_at": created_at,
        "accepted_at": None,
        "source_snapshot_id": str(config.get("source_snapshot_id", "")),
        "source_snapshot": source_snapshot,
        "generator": {
            "name": MODULE,
            "version": generator_version,
        },
        "description": str(config.get("description", "")).strip(),
        "notes": str(config.get("notes", "")).strip(),
    }


def render_build_yaml(store_id: str, config: Mapping, complete: Mapping | None = None) -> dict:
    """A Build Recipe. `complete` adds the completion phase beside the unchanged build (ADR 0037).

    `post` serves both phases; `complete-dense` builds the Top-Hit Index
    inline, so the planner gives the completion phase no `top_hits` step.
    """
    build: dict = {
        "store_id": store_id,
        "layout": "dense",
        "completion_state": "reference_completed" if complete else "observed_only",
        "build": {
            "command": "build-dense-vcf",
            "options": dict(config.get("build_options") or {}),
        },
    }
    if complete:
        build["complete"] = dict(complete)
    build["post"] = dict(config.get("post") or {"top_hits": True, "overview": True})
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
    reports: Mapping[str, str] | None = None,
    completion_reference: Mapping[str, object] | None = None,
) -> dict:
    """The candidate's Validation Record.

    A declared `completion_reference` is recorded under `acceptance`, with
    whether it blocks completion, as #308 recorded the dbGaP MVP references:
    an observed-only bundle then says why it is one.
    """
    status = "passed_with_warnings" if warnings else "passed"
    validation: dict = {
        "status": status,
        "validated_at": generated_at,
        "validator": {"name": MODULE, "version": None},
    }
    if completion_reference is not None:
        validation["acceptance"] = {"completion_reference": dict(completion_reference)}
    return validation | {
        "checks": {
            "schema": "passed",
            "files": "passed",
            "reader_smoke_test": "not_run",
            "ancestry": "passed" if ancestry_checked else "not_run",
            "effect_scale": "not_run",
            "sd_estimation": "not_run",
        },
        "reports": dict(reports or {}),
        "warnings": list(warnings),
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_shared_schema(rows: Sequence[Mapping[str, str]]) -> None:
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
    parser.add_argument("--registry-root", default="stores", help="emit into <registry-root>/<store_id>")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--created-at")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_argv)
    repo_root = Path(args.repo_root)
    if not is_valid_store_id(args.store_id):
        raise SystemExit(f"invalid Store Release id: {args.store_id!r}")

    config = load_config(_resolve(repo_root, args.config))
    document = config.document
    if document.get("store_id", args.store_id) != args.store_id:
        raise CandidateError(
            f"candidate config {config.path} is for {document['store_id']}, not {args.store_id}"
        )
    # Before anything is read or written, so a bad reference emits nothing.
    reference = completion_reference(document)
    complete = completion_block(document, repo_root)
    if reference is not None and complete is None:
        print(
            f"{args.store_id}: completion_reference {reference['resource_id']} is "
            f"qualification {reference['qualification']}, activation {reference['activation']}, "
            "not qualified and active: emitting it observed-only, with no complete block",
            file=sys.stderr,
        )

    inventory_path = Path(args.inventory) if args.inventory else _resolve(repo_root, str(document["inventory"]))
    resolution_path = Path(args.resolution) if args.resolution else _resolve(repo_root, str(document["resolution"]))
    declared_path = Path(args.declared) if args.declared else _resolve(repo_root, str(document["declared"]))

    inventory = read_inventory(inventory_path)
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
    command = f"python3 -m {MODULE} " + " ".join(raw_argv)
    generator_version = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    # `checks.ancestry` is `passed` only when EVERY admitted Analysis has a valid
    # af_assigned record. A partial, extra or source-fallback record set yields
    # `not_run` with a warning rather than a scientific pass.
    admitted_ids = [row["analysis_id"] for row in rows if row["exclude_from_build"] != "true"]
    ancestry_ok, ancestry_warnings = ancestry_validation(admitted_ids, ancestry_records)
    warnings = list(warnings) + ancestry_warnings
    ancestry_identity: dict | None = None
    ancestry_reports: dict[str, str] = {}
    if ancestry_records is not None and args.ancestry_records:
        ancestry_path = Path(args.ancestry_records)
        ancestry_identity = {
            "path": str(ancestry_path),
            "sha256": sha256_file(ancestry_path),
            "rows": len(ancestry_records),
            "admitted_rows": len(admitted_ids),
        }
        ancestry_reports["ancestry"] = str(ancestry_path)

    release = render_release_yaml(
        store_id=args.store_id,
        config=document,
        inventory_path=Path(_display_path(inventory_path, repo_root)),
        inventory_sha256=sha256_file(inventory_path),
        resolution_path=Path(_display_path(resolution_path, repo_root)),
        resolution_sha256=sha256_file(resolution_path),
        n_rows=len(rows),
        n_buildable=n_buildable,
        generator_version=generator_version,
        created_at=created_at,
        ancestry_records_identity=ancestry_identity,
    )
    build = render_build_yaml(args.store_id, document, complete)
    validation = render_validation_yaml(
        n_rows=len(rows),
        n_buildable=n_buildable,
        warnings=warnings,
        ancestry_checked=ancestry_ok,
        generated_at=created_at,
        reports=ancestry_reports,
        completion_reference=None if reference is None else {**reference, "blocked": complete is None},
    )

    _validate_shared_schema(rows)
    emitted = emit(
        CandidateBundle(
            store_id=args.store_id,
            release=release,
            build=build,
            analyses_tsv=render_analyses_tsv(rows),
            commands=[command],
            validation=validation,
        ),
        _resolve(repo_root, args.registry_root),
    )
    print(
        f"Emitted candidate {args.store_id}: {len(rows)} Analyses "
        f"({n_buildable} buildable, {len(rows) - n_buildable} excluded) to {emitted}"
    )
    if warnings:
        for warning in warnings:
            print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
