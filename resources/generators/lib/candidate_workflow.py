"""Phase B candidate workflow: frozen Source Inventory -> candidate Release Bundle (issue #153).

This module is the registry-owned orchestration between the frozen Source
Inventory (issue #151) and an OpenGWASDB Store Release Candidate. It owns four
things and nothing else:

- **deriving the canonical resolver manifest** from the frozen inventory's exact
  ``data_file`` paths and the release's per-study-design method tiers, plus the
  resolved per-Analysis metadata the OpenGWASDB Analysis schema needs;
- **invoking the upstream resolver subprocess** and recording the argv that
  actually ran. The reference panel, the worker pool and per-Analysis
  checkpointing are owned by ``opengwasdb resolve-analyses`` (opengwasdb#208);
  this module never loads a reference, forks a worker, or computes an ancestry,
  allele alignment, or phenotype SD itself;
- **accounting for every record** before anything is written: exactly one record
  per selected Analysis, no missing/stale/duplicate/extra records, and every
  record's source identity and method tier matching the manifest that was
  resolved;
- **applying the registry's release membership and exclusion policy** -- the
  decisions in ADR 0025 (``exclude_from_build`` audit rows), issue #152
  (full-reference ancestry assignment; source-AF-only quantitative estimation;
  non-EUR/unassigned/orientation/SD-unavailable/resolution failures become
  controlled, explained exclusions), and the pinned OpenGWASDB Analysis schema.

Output is a **candidate-only** Release Bundle, assembled deterministically under
a staging sibling and atomically renamed into place. It never builds, validates
or materialises a Store, never redownloads or copies a source body, never
invokes Phase A, and never infers an accepted status.

Where a value is the statistics (ancestry fit, allele alignment, phenotype-SD
estimate) it comes from the resolver record; where a value is a release decision
(membership, method tier, exclusion) it is made here. That line is ADR 0012 /
ADR 0017's, not this module's invention.

See ``docs/spec/store-release-workflow.md`` (Phase B) and
``resources/generators/gwas-catalog-eur-hybrid/README.md``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from resources.generators.lib.ancestry_sidecar import format_sidecar_float
from resources.generators.lib.source_inventory import (
    INVENTORY_COLUMNS,
    PreflightConfigError,
    ReleaseConfiguration,
    SourceInventoryRow,
    duplicate_content_groups,
    load_release_configuration,
)

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

#: The only Release Status a Phase B candidate workflow may write.
CANDIDATE_STATUS: str = "candidate"

#: The super-population vocabulary Assigned Ancestry must use. Re-stated from
#: ``ogstores.bundle`` rather than imported so this generator does not depend on
#: the Phase A package at import time; a drift is caught by ``bundle.check()``.
SUPERPOPULATIONS: tuple[str, ...] = ("AFR", "AMR", "EAS", "EUR", "MID", "NAF", "SAS")

#: ``opengwasdb resolve-analyses`` columns this module writes, in file order.
#: The resolver reads these names (or their documented aliases); the checksum is
#: emitted canonically as ``checksum``.
RESOLVER_MANIFEST_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "source_file",
    "source_reader_capability",
    "stored_effect_scale",
    "original_sd_method",
    "sample_size",
    "checksum",
    "checksum_algorithm",
    "size_bytes",
)

#: The canonical Release Bundle ``analyses.tsv`` columns this generator emits.
#: Shared-core columns come from OpenGWASDB's Analysis schema (ADR 0017);
#: registry-only columns explain membership (ADR 0025).
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
    "source_url",
    "source_bundle_id",
    "downloaded_file",
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

#: ``sidecars/source_readiness.tsv``: the frozen inventory's 14 columns plus the
#: two derived columns that make it a membership audit rather than a copy.
SOURCE_READINESS_COLUMNS: tuple[str, ...] = INVENTORY_COLUMNS + (
    "duplicate_content_group",
    "candidate_membership",
)

ANCESTRY_SIDECAR_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "source_analysis_id",
    "source_ancestry_label",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "ancestry_reference_id",
    "af_overlap",
    "dominant_superpop",
    "dominant_proportion",
    "runner_up_margin",
    "nnls_residual",
    "gate_reason",
    "eaf_orientation",
    "eaf_orientation_r",
    "source_assigned_mismatch",
    "ancestry_notes",
) + tuple(f"ancestry_prop_{superpop}" for superpop in SUPERPOPULATIONS)

SD_ESTIMATION_SIDECAR_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "source_analysis_id",
    "status",
    "skip_reason",
    "af_source",
    "ancestry_reference_id",
    "original_sd",
    "original_sd_method",
    "n_variants_considered",
    "n_variants_overlapping",
    "n_variants_excluded_ambiguous",
    "n_variants_excluded_mismatch",
    "n_variants_excluded_missing_af",
    "n_variants_excluded_maf",
    "n_variants_retained",
    "maf_min",
    "maf_max",
    "implied_sd_median",
    "sd_dispersion",
    "estimator_version",
    "sd_notes",
)

EXCLUSION_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "source_analysis_id",
    "study_design",
    "category",
    "reason",
    "detail",
    "resolver_status",
    "exclude_from_build",
)

#: Controlled exclusion vocabulary (issue #152). Every excluded ready Analysis
#: carries exactly one of these, so "why is this Analysis absent" has one spelling.
EXCLUSION_REASONS: frozenset[str] = frozenset(
    {
        "resolution_failed",
        "ancestry_unassigned",
        "ancestry_not_eur",
        "orientation_failure",
        "sd_no_reference_resource_for_ancestry",
        "sd_no_qualifying_evidence",
        "sd_no_usable_sample_size",
        "sd_failed",
        "missing_sample_size",
        "missing_case_control_counts",
    }
)

#: Exclusion reason -> sidecar ``category`` value.
EXCLUSION_CATEGORIES: Mapping[str, str] = {
    "resolution_failed": "resolution",
    "ancestry_unassigned": "ancestry",
    "ancestry_not_eur": "ancestry",
    "orientation_failure": "orientation",
    "sd_no_reference_resource_for_ancestry": "effect_scale",
    "sd_no_qualifying_evidence": "effect_scale",
    "sd_no_usable_sample_size": "effect_scale",
    "sd_failed": "effect_scale",
    "missing_sample_size": "metadata",
    "missing_case_control_counts": "metadata",
}

#: Resolver record statuses that this module treats as a completed resolution.
_RESOLVER_RECORD_STATUSES: tuple[str, ...] = ("success", "controlled_failure")

#: Path (relative to the repository root) of the tracked Source Ancestry Label
#: -> super-population map, read exactly as ``ancestry.R``'s helper reads it.
SOURCE_LABEL_MAP_RELATIVE_PATH: str = (
    "resources/reference-resources/ukb-ancestry-mixture-hg38/source_label_map.tsv"
)

#: Staging parent used for the atomic directory swap, kept inside the registry
#: root so the rename is same-filesystem, and hidden so a half-written candidate
#: is never discovered by ``bundle-check``/``index`` (which require release.yaml).
STAGING_DIRNAME: str = ".staging"

#: The upstream resolver this module invokes; overridable only for tests through
#: ``--resolver`` so production always uses the pinned ``opengwasdb`` CLI.
DEFAULT_RESOLVER_BIN: str = "opengwasdb"
RESOLVER_SUBCOMMAND: str = "resolve-analyses"


class CandidateError(ValueError):
    """A candidate could not be derived, resolved, verified or finalised safely."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateConfiguration:
    """The full-release config reduced to the facts candidate generation needs.

    ``base`` is the #151 ``ReleaseConfiguration`` (inventory, method tiers,
    Reference Resources, runtime). Everything else here is the release identity
    and Build Recipe shape the Candidate Bundle carries, plus the raw blocks this
    module passes to the resolver unchanged.
    """

    base: ReleaseConfiguration
    label: str
    access_posture: str
    description: str
    notes: str
    defaults: Mapping[str, Any]
    layout: str
    completion_state: str
    build_command: str
    build_options: Mapping[str, Any]
    post: Mapping[str, Any]
    ancestry_block: Mapping[str, Any]
    effect_scale_block: Mapping[str, Any]
    reader_capability: str
    target_ancestry: str


def _require(document: Mapping[str, Any], key: str, where: str) -> Any:
    value = document.get(key)
    if value is None or value == "":
        raise PreflightConfigError(f"{where} is missing required key {key!r}")
    return value


def load_candidate_configuration(path: Path, repo_root: Path) -> CandidateConfiguration:
    """Load the release config and the candidate-specific facts it declares.

    The #151 facts are loaded by ``load_release_configuration`` unchanged, so a
    candidate and a preflight cannot disagree about the inventory, method tiers,
    Reference Resources or runtime. Candidate-specific keys that would otherwise
    be implicit -- the release identity, the Hybrid Build Recipe, and the
    Source Ancestry Label -> Assigned Ancestry translation -- are checked, never
    defaulted to a guessed value.
    """
    base = load_release_configuration(path, repo_root)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise PreflightConfigError(f"release config {path} is not a YAML mapping")

    label = str(_require(document, "label", str(path)))
    access_posture = str(document.get("access_posture") or "public")
    if access_posture not in {"public", "controlled", "embargoed"}:
        raise PreflightConfigError(
            f"{path}: access_posture {access_posture!r} is not public/controlled/embargoed"
        )

    defaults = _require(document, "defaults", str(path))
    build = document.get("build") or {}
    if not isinstance(build, dict):
        raise PreflightConfigError(f"{path}:build must be a mapping")
    options = build.get("options") or {}
    if not isinstance(options, dict):
        raise PreflightConfigError(f"{path}:build.options must be a mapping")
    post = build.get("post") or {}
    if not isinstance(post, dict):
        raise PreflightConfigError(f"{path}:build.post must be a mapping")

    reader_capability = str(options.get("source-reader-capability") or "opengwasdb.gwas-ssf")
    target_ancestry = read_source_label_map(repo_root, base.ancestry_group)

    return CandidateConfiguration(
        base=base,
        label=label,
        access_posture=access_posture,
        description=str(document.get("description") or ""),
        notes=str(document.get("notes") or ""),
        defaults=defaults,
        layout=str(build.get("layout") or "hybrid"),
        completion_state=str(build.get("completion_state") or "observed_only"),
        build_command=str(build.get("command") or "build-hybrid"),
        build_options=dict(options),
        post=dict(post),
        ancestry_block=_mapping(document.get("ancestry_assignment") or {}),
        effect_scale_block=_mapping(document.get("effect_scale_validation") or {}),
        reader_capability=reader_capability,
        target_ancestry=target_ancestry,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def read_source_label_map(repo_root: Path, label: str) -> str:
    """Translate a Source Ancestry Label to its super-population code.

    A label absent from the tracked map is an error, not a silent pass-through:
    an unnormalised value is not a valid Assigned Ancestry (issue #133).
    """
    path = repo_root / SOURCE_LABEL_MAP_RELATIVE_PATH
    if not path.is_file():
        raise PreflightConfigError(f"tracked Source Ancestry Label map not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    mapping = {
        (row.get("source_label") or "").strip(): (row.get("super_population") or "").strip()
        for row in rows
    }
    code = mapping.get(label)
    if not code:
        raise PreflightConfigError(
            f"no tracked Source Ancestry Label -> super-population mapping for {label!r} "
            f"in {path}"
        )
    if code not in SUPERPOPULATIONS:
        raise PreflightConfigError(
            f"{path} maps {label!r} to {code!r}, which is not a super-population code"
        )
    return code


# ---------------------------------------------------------------------------
# Resolved per-Analysis metadata (the candidate table)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateMetadata:
    """Resolved Analytical Metadata for one Analysis, from the candidate table.

    The frozen Source Inventory (#151) records selection identity, the exact
    source path and readiness; it deliberately does not carry the resolved
    labels, publication identity, case/control split or total N. Those are the
    metadata resolver's output (`resources/scripts/ebi-studies.r`), materialised
    into the candidate table the freeze already accounts every inventory row
    against and checksums into the provenance sidecar. Reading them here is a
    join on an already-frozen selection, not a second selection: membership is
    still the inventory's.
    """

    analysis_id: str
    source_label: str
    trait_ontology_label: str
    trait_ontology_id: str
    trait_ontology_mapping_method: str
    publication_pmid: str
    first_author: str
    n_cases: str
    n_controls: str
    sample_size: str


def _obo_uri_to_curie(uri: str) -> str:
    """Turn an OBO PURL into a CURIE, or pass any other id through unchanged.

    ``http://purl.obolibrary.org/obo/MONDO_0005148`` -> ``MONDO:0005148``. A URI
    that does not match the PURL shape is left verbatim rather than guessed.
    """
    value = (uri or "").strip()
    prefix = "http://purl.obolibrary.org/obo/"
    if value.startswith(prefix):
        tail = value[len(prefix):]
        if "_" in tail:
            namespace, _, local = tail.partition("_")
            if namespace and local:
                return f"{namespace}:{local}"
    return value


def read_candidate_metadata(
    path: Path, analysis_ids: Iterable[str]
) -> dict[str, CandidateMetadata]:
    """Read resolved metadata for ``analysis_ids`` from the candidate table.

    A selected ready Analysis with no row here cannot have schema-valid metadata,
    so it fails loudly rather than being emitted with a guessed label or a
    fabricated case count.
    """
    if not path.is_file():
        raise CandidateError(f"candidate metadata table not found: {path}")
    wanted = set(analysis_ids)
    found: dict[str, CandidateMetadata] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fieldnames = reader.fieldnames or []
        if "STUDY.ACCESSION" not in fieldnames:
            raise CandidateError(
                f"candidate metadata table {path} is missing required column 'STUDY.ACCESSION'"
            )
        for raw in reader:
            accession = (raw.get("STUDY.ACCESSION") or "").strip()
            if accession not in wanted:
                continue
            mapped_uri = (raw.get("MAPPED_TRAIT_URI") or "").strip()
            mapped_label = (raw.get("MAPPED_TRAIT") or "").strip()
            if mapped_uri and mapped_label:
                ontology_id = _obo_uri_to_curie(mapped_uri)
                ontology_label = mapped_label
                method = "source_provided"
            else:
                ontology_id = ""
                ontology_label = ""
                method = "unmapped"
            found[accession] = CandidateMetadata(
                analysis_id=accession,
                source_label=(raw.get("DISEASE.TRAIT") or "").strip(),
                trait_ontology_label=ontology_label,
                trait_ontology_id=ontology_id,
                trait_ontology_mapping_method=method,
                publication_pmid=(raw.get("PUBMED.ID") or "").strip(),
                first_author=(raw.get("FIRST.AUTHOR") or "").strip(),
                n_cases=_clean_number(raw.get("n_cases")),
                n_controls=_clean_number(raw.get("n_controls")),
                sample_size=_clean_number(raw.get("sample_size")),
            )
    missing = sorted(wanted - set(found))
    if missing:
        raise CandidateError(
            f"candidate metadata table {path} has no row for {len(missing)} selected "
            f"ready Analysis/Analyses: {', '.join(missing[:10])}"
        )
    return found


def _clean_number(value: str | None) -> str:
    """Normalise a numeric cell to its canonical string, or empty when absent."""
    text = (value or "").strip()
    if not text or text.lower() in {"na", "nan", "null", "none"}:
        return ""
    return text


# ---------------------------------------------------------------------------
# Canonical resolver manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolverRow:
    """One row of the canonical resolver manifest."""

    analysis_id: str
    source_file: str
    source_reader_capability: str
    stored_effect_scale: str
    original_sd_method: str
    sample_size: str
    checksum: str
    checksum_algorithm: str
    size_bytes: str

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in RESOLVER_MANIFEST_COLUMNS}


def derive_resolver_manifest(
    rows: Sequence[SourceInventoryRow],
    config: CandidateConfiguration,
    metadata: Mapping[str, CandidateMetadata],
) -> list[ResolverRow]:
    """Derive the canonical resolver manifest from the frozen exact paths.

    Every ready inventory row becomes exactly one manifest row. The method tier
    comes from ``defaults.by_study_design`` (a design with no tier is a
    preflight failure, so it cannot reach here), the source identity comes from
    the inventory's recorded ``data_file``/``sha256``/``data_bytes`` -- never
    reconstructed from an accession -- and the total N comes from the resolved
    metadata. ``data_file`` is used verbatim; nothing here opens it.
    """
    manifest: list[ResolverRow] = []
    for row in rows:
        if not row.ready:
            continue
        tier = config.base.method_tiers.get(row.study_design)
        if tier is None:
            raise CandidateError(
                f"{row.analysis_id}: no declared method tier for study_design "
                f"{row.study_design!r}; preflight should have failed first"
            )
        if not row.data_file.strip():
            raise CandidateError(f"{row.analysis_id}: ready row has no data_file")
        if not row.sha256.strip():
            raise CandidateError(f"{row.analysis_id}: ready row has no sha256")
        resolved = metadata.get(row.analysis_id)
        sample_size = resolved.sample_size if resolved else ""
        manifest.append(
            ResolverRow(
                analysis_id=row.analysis_id,
                source_file=row.data_file,
                source_reader_capability=config.reader_capability,
                stored_effect_scale=tier.stored_effect_scale,
                original_sd_method=tier.original_sd_method,
                sample_size=sample_size,
                checksum=row.sha256,
                checksum_algorithm="sha256",
                size_bytes=row.data_bytes,
            )
        )
    if not manifest:
        raise CandidateError("no ready Analysis was selected; refusing to emit an empty candidate")
    return manifest


def render_resolver_manifest(rows: Sequence[ResolverRow]) -> str:
    """Render the resolver manifest TSV, one row per ready selected Analysis."""
    return _render_tsv(RESOLVER_MANIFEST_COLUMNS, [row.as_dict() for row in rows])


def _resolve_af_reference_specs(config: CandidateConfiguration) -> list[str]:
    """Render ``ancestry=path`` specs for each declared reference-AF resource.

    This release (issue #152) declares none, so the list is empty and the
    resolver's reference-MAF tier stays disabled; a future release that declares
    a fallback gets the flag without this module acquiring a reference path of
    its own.
    """
    specs: list[str] = []
    for ancestry, resource_id in config.base.effect_scale_reference_resources:
        resource = config.base.reference_resources.get(resource_id)
        if resource is None or not resource.location:
            continue
        specs.append(f"{ancestry}={resource.location}")
    return specs


def resolver_argv(
    *,
    resolver_bin: str,
    manifest_path: Path,
    records_dir: Path,
    config: CandidateConfiguration,
    cores: int,
    resume: bool,
) -> list[str]:
    """Compose the exact ``opengwasdb resolve-analyses`` argv.

    Only registry facts and declared config values are composed: the manifest
    and records paths, the declared ancestry reference and its fine-group map,
    the declared gates, and the worker count. Everything else is the resolver's
    own default. Per ADR 0023 the registry names a subcommand and passes flags
    through, and does not mirror a parameter schema.
    """
    ancestry_resource = config.base.reference_resources.get(
        config.base.ancestry_reference_resource_id
    )
    if ancestry_resource is None:
        raise CandidateError(
            f"ancestry_assignment.reference_resource_id "
            f"{config.base.ancestry_reference_resource_id!r} is not a declared Reference Resource"
        )
    fine_group_map = dict(ancestry_resource.auxiliary_paths).get("fine_group_map")
    if not fine_group_map:
        raise CandidateError(
            f"Reference Resource {ancestry_resource.resource_id!r} declares no fine_group_map"
        )

    gates = _mapping(config.ancestry_block.get("gates"))
    argv: list[str] = [
        resolver_bin,
        RESOLVER_SUBCOMMAND,
        str(manifest_path),
        str(records_dir),
        "--ancestry-reference",
        ancestry_resource.location,
        "--ancestry-groups",
        fine_group_map,
    ]
    extraction_panel = config.ancestry_block.get("extraction_panel")
    if extraction_panel:
        argv.extend(["--extraction-panel", str(extraction_panel)])
    for spec in _resolve_af_reference_specs(config):
        argv.extend(["--af-reference", spec])
    argv.extend(
        [
            "--default-source-reader-capability",
            config.reader_capability,
            "--maf-floor",
            str(config.ancestry_block.get("maf_floor", 0.01)),
            "--tau",
            str(gates.get("tau", 0.50)),
            "--delta",
            str(gates.get("delta", 0.20)),
            "--n-min",
            str(gates.get("n_min", 5000)),
            "--residual-max",
            str(gates.get("residual_max", 0.06)),
            "--n-workers",
            str(cores),
        ]
    )
    if resume:
        argv.append("--resume")
    return argv


# ---------------------------------------------------------------------------
# Resolver execution and record accounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolverRun:
    """What one resolver invocation did, for the evidence log."""

    argv: list[str]
    returncode: int
    log_path: Path


def run_resolver(argv: Sequence[str], *, cwd: Path, log_path: Path) -> ResolverRun:
    """Run the resolver subprocess, teeing its output to a durable log.

    The registry does not parse the resolver's stdout for accounting: the
    accounting comes from the records and ``index.json``, which are the
    resolver's declared contract. The raw output is kept so a failure is
    auditable without re-running it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    log_path.write_text(completed.stdout or "", encoding="utf-8")
    return ResolverRun(argv=list(argv), returncode=completed.returncode, log_path=log_path)


def _recompute_fingerprint_digest(fingerprints: Mapping[str, Any]) -> str:
    """Recompute the resolver's canonical fingerprint digest over a record.

    This mirrors ``opengwasdb.build.resolve_manifest.compute_fingerprint_digest``:
    a SHA-256 over the JSON of the fingerprint mapping with the digest itself
    removed, sorted keys and compact separators. Recomputing it here is not
    duplicating the resolver's statistics -- it is the registry checking that the
    bytes it is about to freeze are the bytes the resolver wrote.
    """
    clean = {key: value for key, value in fingerprints.items() if key != "fingerprint_digest"}
    payload = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _expected_resolution_fields(row: ResolverRow) -> dict[str, Any]:
    return {
        "source_file": row.source_file,
        "source_recorded_sha256": row.checksum or None,
        "source_recorded_bytes": int(row.size_bytes) if row.size_bytes else None,
        "stored_effect_scale": row.stored_effect_scale,
        "original_sd_method": row.original_sd_method,
        "source_reader_capability": row.source_reader_capability,
        "sample_size": float(row.sample_size) if row.sample_size else None,
    }


#: Resolver fingerprint keys recorded at the mapping's top level.
_FINGERPRINT_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "source_file",
    "source_recorded_sha256",
    "source_recorded_bytes",
)

#: Resolver fingerprint keys recorded inside ``resolution_config``.
_FINGERPRINT_CONFIG_KEYS: tuple[str, ...] = (
    "stored_effect_scale",
    "original_sd_method",
    "source_reader_capability",
    "sample_size",
)


def verify_records(
    manifest_rows: Sequence[ResolverRow],
    records_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Account every record against the manifest that was resolved.

    Returns ``(records, failures)`` where ``records`` is in manifest order and
    ``failures`` lists every way the records are missing, stale, duplicated or
    extra. An empty failure list means finalisation may proceed. Any failure
    means the candidate is not written: a partial or stale record set is exactly
    the silently-wrong bundle this repository exists to prevent.
    """
    failures: list[str] = []
    expected_ids = [row.analysis_id for row in manifest_rows]
    expected_set = set(expected_ids)
    duplicate_ids = sorted({aid for aid in expected_ids if expected_ids.count(aid) > 1})
    if duplicate_ids:
        failures.append(f"resolver manifest has duplicate analysis_id: {', '.join(duplicate_ids)}")

    index_path = records_dir / "index.json"
    if not index_path.is_file():
        failures.append(f"resolver index is missing: {index_path}")
        return [], failures
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        failures.append(f"resolver index {index_path} is not valid JSON: {exc}")
        return [], failures

    recorded = index.get("analyses")
    if not isinstance(recorded, list):
        failures.append(f"resolver index {index_path} has no 'analyses' list")
        return [], failures
    index_ids = [str(entry.get("analysis_id", "")) for entry in recorded if isinstance(entry, dict)]
    if index_ids != expected_ids:
        failures.append(
            f"resolver index records {len(index_ids)} Analyses but the manifest resolved "
            f"{len(expected_ids)}; order/identity differs "
            f"(first mismatch: {_first_mismatch(index_ids, expected_ids)})"
        )
    if index.get("n_total") != len(expected_ids):
        failures.append(
            f"resolver index n_total {index.get('n_total')!r} does not match the "
            f"{len(expected_ids)} manifest rows"
        )
    duplicate_index_ids = sorted({aid for aid in index_ids if index_ids.count(aid) > 1})
    if duplicate_index_ids:
        failures.append(f"resolver index has duplicate analysis_id: {', '.join(duplicate_index_ids)}")

    # Extra record files: a records dir reused across a changed manifest leaves
    # records the current manifest does not account for.
    extra = sorted(
        path.name
        for path in records_dir.glob("*.json")
        if path.name != "index.json"
        and path.stem not in expected_set
        and not path.name.startswith(".tmp_")
    )
    if extra:
        failures.append(
            f"{len(extra)} extra resolver record(s) for analyses outside the manifest: "
            f"{', '.join(extra[:10])}"
        )

    records: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in manifest_rows:
        record_path = records_dir / f"{row.analysis_id}.json"
        if not record_path.is_file():
            failures.append(f"{row.analysis_id}: resolver record is missing ({record_path})")
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{row.analysis_id}: resolver record is not valid JSON: {exc}")
            continue
        if record.get("analysis_id") != row.analysis_id:
            failures.append(
                f"{row.analysis_id}: resolver record names analysis_id "
                f"{record.get('analysis_id')!r}"
            )
            continue
        status = record.get("status")
        if status not in _RESOLVER_RECORD_STATUSES:
            failures.append(
                f"{row.analysis_id}: resolver record has unexpected status {status!r}"
            )
            continue
        failures.extend(_verify_fingerprints(row, record))
        by_id[row.analysis_id] = record

    for row in manifest_rows:
        if row.analysis_id in by_id:
            records.append(by_id[row.analysis_id])
    return records, failures


def _first_mismatch(actual: Sequence[str], expected: Sequence[str]) -> str:
    for left, right in zip(actual, expected):
        if left != right:
            return f"{left!r} != {right!r}"
    return f"length {len(actual)} != {len(expected)}"


def _verify_fingerprints(row: ResolverRow, record: Mapping[str, Any]) -> list[str]:
    """Check a record's source identity and tier against the manifest row.

    A record whose recorded source checksum, size, file, tier or reader
    capability no longer matches the manifest was produced against a different
    input than the one being finalised. With ``--resume`` the resolver re-runs
    such records; if one survives to here it is stale, and a stale record must
    fail finalisation rather than be frozen into a candidate.
    """
    failures: list[str] = []
    fingerprints = record.get("fingerprints")
    if not isinstance(fingerprints, Mapping):
        return [f"{row.analysis_id}: resolver record has no fingerprints mapping"]
    digest = fingerprints.get("fingerprint_digest")
    if digest != _recompute_fingerprint_digest(fingerprints):
        failures.append(
            f"{row.analysis_id}: resolver fingerprint digest does not match its own "
            "fingerprint inputs (record was edited or is stale)"
        )
    expected = _expected_resolution_fields(row)
    for key in _FINGERPRINT_TOP_LEVEL_KEYS:
        actual = fingerprints.get(key)
        if actual != expected[key]:
            failures.append(
                f"{row.analysis_id}: resolver fingerprint {key} is {actual!r}, "
                f"expected {expected[key]!r}"
            )
    resolution_config = fingerprints.get("resolution_config")
    if not isinstance(resolution_config, Mapping):
        failures.append(f"{row.analysis_id}: resolver fingerprint has no resolution_config")
    else:
        for key in _FINGERPRINT_CONFIG_KEYS:
            if resolution_config.get(key) != expected[key]:
                failures.append(
                    f"{row.analysis_id}: resolver resolution_config.{key} is "
                    f"{resolution_config.get(key)!r}, expected {expected[key]!r}"
                )
    return failures


# ---------------------------------------------------------------------------
# Release membership and exclusion policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisOutcome:
    """One selected ready Analysis after the release policy was applied."""

    row: SourceInventoryRow
    metadata: CandidateMetadata
    record: Mapping[str, Any] | None
    included: bool
    exclusion_reason: str
    exclusion_detail: str
    assigned_ancestry: str
    ancestry_assignment_method: str
    original_sd: str
    original_sd_method: str
    stored_effect_scale: str
    sample_size: str
    n_cases: str
    n_controls: str

    @property
    def analysis_id(self) -> str:
        return self.row.analysis_id


def _record_mapping(record: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    value = (record or {}).get(key)
    return value if isinstance(value, Mapping) else {}


def apply_release_policy(
    rows: Sequence[SourceInventoryRow],
    config: CandidateConfiguration,
    metadata: Mapping[str, CandidateMetadata],
    records: Mapping[str, Mapping[str, Any]],
) -> list[AnalysisOutcome]:
    """Decide membership for every selected ready Analysis, with a reason.

    The order matters and encodes the #152 decision: an Analysis that cannot be
    given a trustworthy Assigned Ancestry, or whose effects cannot be put on the
    release's declared scale, becomes an ``exclude_from_build`` audit row with
    one controlled reason -- never a silently dropped row and never a fabricated
    value. Duplicate-content accessions are not collapsed: each keeps its own
    row and its own decision.
    """
    outcomes: list[AnalysisOutcome] = []
    for row in rows:
        if not row.ready:
            continue
        outcomes.append(
            _decide(
                row=row,
                resolved=metadata[row.analysis_id],
                tier=config.base.method_tiers[row.study_design],
                record=records.get(row.analysis_id),
                config=config,
            )
        )
    return outcomes


def _decide(
    *,
    row: SourceInventoryRow,
    resolved: CandidateMetadata,
    tier: Any,
    record: Mapping[str, Any] | None,
    config: CandidateConfiguration,
) -> AnalysisOutcome:
    stored_effect_scale = tier.stored_effect_scale
    case_control = stored_effect_scale in {"log_or", "log_hazard"}
    sample_size = resolved.sample_size
    n_cases = resolved.n_cases if case_control else ""
    n_controls = resolved.n_controls if case_control else ""

    def make(
        *,
        included: bool,
        reason: str = "",
        detail: str = "",
        assigned: str = "",
        method: str = "unassigned",
        original_sd: str = "",
        sd_method: str | None = None,
    ) -> AnalysisOutcome:
        return AnalysisOutcome(
            row=row,
            metadata=resolved,
            record=record,
            included=included,
            exclusion_reason=reason,
            exclusion_detail=detail,
            assigned_ancestry=assigned,
            ancestry_assignment_method=method,
            original_sd=original_sd,
            original_sd_method=sd_method or tier.original_sd_method,
            stored_effect_scale=stored_effect_scale,
            sample_size=sample_size,
            n_cases=n_cases,
            n_controls=n_controls,
        )

    if not sample_size or _non_positive(sample_size):
        return make(
            included=False,
            reason="missing_sample_size",
            detail="resolved sample_size is empty or non-positive",
        )

    if record is None:
        return make(included=False, reason="resolution_failed", detail="no resolver record")
    if record.get("status") != "success":
        error = str(record.get("error") or "resolver returned controlled_failure")
        return make(included=False, reason="resolution_failed", detail=error)

    ancestry = _record_mapping(record, "ancestry")
    assigned = str(ancestry.get("assigned_ancestry") or "").strip()
    gate_reason = str(ancestry.get("gate_reason") or "").strip()
    eaf_orientation = str(ancestry.get("eaf_orientation") or "").strip().lower()

    if eaf_orientation and eaf_orientation not in {"ok", "consistent", "none"}:
        return make(
            included=False,
            reason="orientation_failure",
            detail=(
                f"eaf_orientation={eaf_orientation!r} "
                f"r={ancestry.get('eaf_orientation_r')!r}"
            ),
            assigned=assigned,
        )
    if gate_reason == "eaf_orientation":
        return make(
            included=False,
            reason="orientation_failure",
            detail=f"gate_reason={gate_reason!r} r={ancestry.get('eaf_orientation_r')!r}",
            assigned=assigned,
        )
    if not assigned:
        return make(
            included=False,
            reason="ancestry_unassigned",
            detail=f"gate_reason={gate_reason or 'none'}",
        )
    if assigned not in SUPERPOPULATIONS:
        return make(
            included=False,
            reason="ancestry_unassigned",
            detail=f"assigned_ancestry {assigned!r} is not a super-population code",
        )
    if assigned != config.target_ancestry:
        return make(
            included=False,
            reason="ancestry_not_eur",
            detail=(
                f"assigned_ancestry={assigned} (release target {config.target_ancestry})"
            ),
            assigned=assigned,
            method="af_assigned",
        )

    # From here the Analysis has a trustworthy target ancestry; the only
    # remaining question is whether its effects are on the release's scale.
    if case_control:
        if not _positive(n_cases) or not _positive(n_controls):
            return make(
                included=False,
                reason="missing_case_control_counts",
                detail=f"n_cases={n_cases or 'empty'} n_controls={n_controls or 'empty'}",
                assigned=assigned,
                method="af_assigned",
            )
        return make(
            included=True,
            assigned=assigned,
            method="af_assigned",
            original_sd="",
            sd_method="binary_trait",
        )

    pheno = _record_mapping(record, "phenotype_sd")
    status = str(pheno.get("status") or "").strip()
    reason = str(pheno.get("reason") or "").strip()
    estimate = pheno.get("estimate")
    if status == "estimated" and isinstance(estimate, Mapping) and estimate.get("sd") is not None:
        sd_value = float(estimate["sd"])
        return make(
            included=True,
            assigned=assigned,
            method="af_assigned",
            original_sd=format_sidecar_float(sd_value),
            sd_method=str(estimate.get("method") or tier.original_sd_method),
        )
    if status == "skipped" and reason == "no_reference_resource_for_ancestry":
        return make(
            included=False,
            reason="sd_no_reference_resource_for_ancestry",
            detail=reason,
            assigned=assigned,
            method="af_assigned",
        )
    if status == "skipped":
        return make(
            included=False,
            reason="sd_no_reference_resource_for_ancestry" if not reason else _sd_reason(reason),
            detail=reason or "quantitative SD estimation was skipped",
            assigned=assigned,
            method="af_assigned",
        )
    if status == "unavailable":
        return make(
            included=False,
            reason=_sd_reason(reason),
            detail=reason or "no phenotype SD could be estimated",
            assigned=assigned,
            method="af_assigned",
        )
    return make(
        included=False,
        reason="sd_failed",
        detail=f"phenotype_sd status={status or 'missing'!r} reason={reason or 'none'!r}",
        assigned=assigned,
        method="af_assigned",
    )


def _sd_reason(reason: str) -> str:
    mapping = {
        "no_reference_resource_for_ancestry": "sd_no_reference_resource_for_ancestry",
        "no_qualifying_evidence": "sd_no_qualifying_evidence",
        "no_usable_sample_size": "sd_no_usable_sample_size",
    }
    return mapping.get(reason, "sd_failed")


def _non_positive(value: str) -> bool:
    try:
        return float(value) <= 0
    except ValueError:
        return True


def _positive(value: str) -> bool:
    """True only for a parseable, strictly positive count; zero is absence here."""
    try:
        return float(value) > 0
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Bundle table and sidecar rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateTables:
    """The deterministic, byte-stable bundle files for one candidate."""

    analyses_tsv: str
    source_readiness_tsv: str
    ancestry_tsv: str
    sd_estimation_tsv: str
    exclusions_tsv: str
    inventory_rows: int
    included_rows: int
    excluded_rows: int
    exclusion_counts: Mapping[str, int]
    ancestry_check: str
    sd_check: str
    effect_scale_check: str
    warnings: tuple[str, ...]


def build_candidate_tables(
    *,
    inventory_rows: Sequence[SourceInventoryRow],
    outcomes: Sequence[AnalysisOutcome],
    config: CandidateConfiguration,
    index_summary: Mapping[str, Any],
) -> CandidateTables:
    """Render every bundle table/sidecar in manifest order, deterministically.

    Nothing here depends on completion order or worker count: rows are indexed
    by the frozen inventory's order and every derived value is a pure function
    of the inventory, the candidate metadata, and the resolver record.
    """
    duplicate_membership: dict[str, str] = {}
    for group in duplicate_content_groups(inventory_rows):
        label = "+".join(group.analysis_ids)
        for analysis_id in group.analysis_ids:
            duplicate_membership[analysis_id] = label

    outcome_by_id = {outcome.analysis_id: outcome for outcome in outcomes}
    analyses_rows = [
        _analyses_row(index, outcome, config) for index, outcome in enumerate(outcomes)
    ]

    readiness_rows: list[dict[str, str]] = []
    for row in inventory_rows:
        membership = "not_ready"
        if row.ready:
            membership = "included" if outcome_by_id[row.analysis_id].included else "excluded"
        record = row.as_dict()
        record["duplicate_content_group"] = duplicate_membership.get(row.analysis_id, "")
        record["candidate_membership"] = membership
        readiness_rows.append(record)

    ancestry_rows = [
        _ancestry_row(outcome, config) for outcome in outcomes
    ]
    sd_rows = [_sd_row(outcome, config, index_summary) for outcome in outcomes]
    exclusion_rows = [
        _exclusion_row(outcome) for outcome in outcomes if not outcome.included
    ]

    exclusion_counts: dict[str, int] = {}
    for outcome in outcomes:
        if not outcome.included:
            exclusion_counts[outcome.exclusion_reason] = (
                exclusion_counts.get(outcome.exclusion_reason, 0) + 1
            )
    unknown = sorted(set(exclusion_counts) - EXCLUSION_REASONS)
    if unknown:
        raise CandidateError(f"internal error: uncontrolled exclusion reason(s): {unknown}")

    ancestry_check, sd_check, effect_scale_check, warnings = _derive_checks(
        outcomes, sd_rows, exclusion_counts, duplicate_membership
    )

    return CandidateTables(
        analyses_tsv=_render_tsv(ANALYSES_COLUMNS, analyses_rows),
        source_readiness_tsv=_render_tsv(SOURCE_READINESS_COLUMNS, readiness_rows),
        ancestry_tsv=_render_tsv(ANCESTRY_SIDECAR_COLUMNS, ancestry_rows),
        sd_estimation_tsv=_render_tsv(SD_ESTIMATION_SIDECAR_COLUMNS, sd_rows),
        exclusions_tsv=_render_tsv(EXCLUSION_COLUMNS, exclusion_rows),
        inventory_rows=len(inventory_rows),
        included_rows=sum(1 for outcome in outcomes if outcome.included),
        excluded_rows=sum(1 for outcome in outcomes if not outcome.included),
        exclusion_counts=dict(sorted(exclusion_counts.items())),
        ancestry_check=ancestry_check,
        sd_check=sd_check,
        effect_scale_check=effect_scale_check,
        warnings=tuple(warnings),
    )


def _analyses_row(
    index: int, outcome: AnalysisOutcome, config: CandidateConfiguration
) -> dict[str, str]:
    row = outcome.row
    resolved = outcome.metadata
    if outcome.included:
        inclusion_reason = "selected_ready_source"
        exclude = ""
    else:
        inclusion_reason = f"excluded: {outcome.exclusion_reason}: {outcome.exclusion_detail}"
        exclude = "true"
    tier = config.base.method_tiers[row.study_design]
    return {
        "analysis_index": str(index),
        "analysis_id": row.analysis_id,
        "source_analysis_id": row.analysis_id,
        "source_label": resolved.source_label or row.trait,
        "analysis_label": resolved.source_label or row.trait,
        "trait_ontology_label": resolved.trait_ontology_label,
        "trait_ontology_id": resolved.trait_ontology_id,
        "trait_ontology_mapping_method": resolved.trait_ontology_mapping_method,
        "source_file": row.data_file,
        "source_url": row.data_url,
        "source_bundle_id": "",
        "downloaded_file": Path(row.data_file).name if row.data_file else "",
        "checksum": row.sha256,
        "checksum_algorithm": "sha256",
        "size_bytes": row.data_bytes,
        "source_genome_build": str(config.defaults.get("source_genome_build", "GRCh38")),
        "license": str(config.defaults.get("license", "")),
        "publication_doi": "",
        "publication_pmid": resolved.publication_pmid or row.publication_pmid,
        "consortium": "",
        "first_author": resolved.first_author,
        "source_ancestry_label": config.base.ancestry_group,
        "assigned_ancestry": outcome.assigned_ancestry,
        "ancestry_assignment_method": outcome.ancestry_assignment_method,
        "original_effect_scale": tier.original_effect_scale,
        "original_sd": outcome.original_sd,
        "original_sd_method": outcome.original_sd_method,
        "stored_effect_scale": outcome.stored_effect_scale,
        "sample_size_kind": tier.sample_size_kind,
        "sample_size_scope": str(config.defaults.get("sample_size_scope", "analysis_level")),
        "sample_size": outcome.sample_size,
        "n_cases": outcome.n_cases,
        "n_controls": outcome.n_controls,
        "analysis_group_id": str(config.defaults.get("analysis_group_id", "")),
        "inclusion_reason": inclusion_reason,
        "exclude_from_build": exclude,
    }


def _ancestry_row(
    outcome: AnalysisOutcome, config: CandidateConfiguration
) -> dict[str, str]:
    record = outcome.record
    ancestry = _record_mapping(record, "ancestry")
    row: dict[str, str] = {name: "" for name in ANCESTRY_SIDECAR_COLUMNS}
    row["analysis_id"] = outcome.analysis_id
    row["source_analysis_id"] = outcome.analysis_id
    row["source_ancestry_label"] = config.base.ancestry_group
    row["assigned_ancestry"] = outcome.assigned_ancestry
    row["ancestry_assignment_method"] = outcome.ancestry_assignment_method
    if record is None:
        row["gate_reason"] = "resolution_failed"
        row["ancestry_notes"] = "no resolver record"
        return row
    fingerprints = record.get("fingerprints")
    if isinstance(fingerprints, Mapping):
        row["ancestry_reference_id"] = str(fingerprints.get("ancestry_reference_id") or "")
    if record.get("status") != "success":
        row["gate_reason"] = "resolution_failed"
        row["ancestry_notes"] = str(record.get("error") or "controlled_failure")
        return row
    if not ancestry:
        row["gate_reason"] = "no_ancestry_result"
        return row
    row["af_overlap"] = _int_or_empty(ancestry.get("af_overlap"))
    row["dominant_superpop"] = str(ancestry.get("dominant_superpop") or "")
    row["dominant_proportion"] = _float_or_empty(ancestry.get("dominant_proportion"))
    row["runner_up_margin"] = _float_or_empty(ancestry.get("runner_up_margin"))
    row["nnls_residual"] = _float_or_empty(ancestry.get("residual"))
    row["gate_reason"] = str(ancestry.get("gate_reason") or "")
    row["eaf_orientation"] = str(ancestry.get("eaf_orientation") or "")
    row["eaf_orientation_r"] = _float_or_empty(ancestry.get("eaf_orientation_r"))
    if outcome.assigned_ancestry:
        row["source_assigned_mismatch"] = (
            "false" if outcome.assigned_ancestry == config.target_ancestry else "true"
        )
    composition = ancestry.get("superpop_composition")
    if isinstance(composition, Mapping):
        for superpop in SUPERPOPULATIONS:
            row[f"ancestry_prop_{superpop}"] = _float_or_empty(composition.get(superpop))
    return row


def _sd_row(
    outcome: AnalysisOutcome,
    config: CandidateConfiguration,
    index_summary: Mapping[str, Any],
) -> dict[str, str]:
    record = outcome.record
    pheno = _record_mapping(record, "phenotype_sd")
    row: dict[str, str] = {name: "" for name in SD_ESTIMATION_SIDECAR_COLUMNS}
    row["analysis_id"] = outcome.analysis_id
    row["source_analysis_id"] = outcome.analysis_id
    row["original_sd_method"] = outcome.original_sd_method
    row["maf_min"] = str(config.effect_scale_block.get("maf_min", ""))
    row["maf_max"] = str(config.effect_scale_block.get("maf_max", ""))
    version = index_summary.get("opengwasdb_version")
    git_hash = index_summary.get("opengwasdb_git_hash")
    row["estimator_version"] = (
        f"opengwasdb@{git_hash}" if git_hash else (f"opengwasdb:{version}" if version else "")
    )

    if outcome.stored_effect_scale in {"log_or", "log_hazard"}:
        row["status"] = "skipped"
        row["skip_reason"] = "non_quantitative_effect_scale"
        row["sd_notes"] = "case-control effect scale; phenotype-SD estimation not applicable"
        return row

    if record is None or record.get("status") != "success":
        row["status"] = "failed"
        row["sd_notes"] = str((record or {}).get("error") or "no resolver record")
        return row

    status = str(pheno.get("status") or "").strip()
    reason = str(pheno.get("reason") or "").strip()
    row["skip_reason"] = reason
    row["af_source"] = _af_source(outcome.original_sd_method)
    row["ancestry_reference_id"] = str(pheno.get("reference_id") or "")
    row["n_variants_considered"] = _int_or_empty(pheno.get("n_evidence_considered"))
    row["n_variants_retained"] = _int_or_empty(pheno.get("n_estimate_inputs"))
    if status == "estimated":
        estimate = pheno.get("estimate")
        if isinstance(estimate, Mapping):
            row["original_sd"] = _float_or_empty(estimate.get("sd"))
            row["implied_sd_median"] = _float_or_empty(estimate.get("sd"))
            row["sd_dispersion"] = _float_or_empty(estimate.get("dispersion"))
            row["sd_notes"] = str(estimate.get("notes") or "")
            dispersion = estimate.get("dispersion")
            dispersion_max = float(config.effect_scale_block.get("dispersion_max", 0.5) or 0.5)
            if dispersion is not None and float(dispersion) > dispersion_max:
                row["status"] = "warning"
            else:
                row["status"] = "passed"
            row["original_sd_method"] = str(estimate.get("method") or outcome.original_sd_method)
        else:
            row["status"] = "failed"
            row["sd_notes"] = "estimated phenotype SD but no estimate payload"
        return row
    if status == "skipped":
        row["status"] = "skipped"
        row["sd_notes"] = f"skipped: {reason or 'unspecified'}"
        return row
    row["status"] = "failed"
    row["sd_notes"] = f"unavailable: {reason or 'unspecified'}"
    return row


def _af_source(original_sd_method: str) -> str:
    if original_sd_method == "estimated_from_source_maf":
        return "source"
    if original_sd_method == "estimated_from_reference_maf":
        return "reference"
    return ""


def _exclusion_row(outcome: AnalysisOutcome) -> dict[str, str]:
    record_status = str((outcome.record or {}).get("status") or "missing")
    return {
        "analysis_id": outcome.analysis_id,
        "source_analysis_id": outcome.analysis_id,
        "study_design": outcome.row.study_design,
        "category": EXCLUSION_CATEGORIES[outcome.exclusion_reason],
        "reason": outcome.exclusion_reason,
        "detail": outcome.exclusion_detail,
        "resolver_status": record_status,
        "exclude_from_build": "true",
    }


def _derive_checks(
    outcomes: Sequence[AnalysisOutcome],
    sd_rows: Sequence[Mapping[str, str]],
    exclusion_counts: Mapping[str, int],
    duplicate_membership: Mapping[str, str],
) -> tuple[str, str, str, list[str]]:
    """Derive the validation checks and warnings from the decided outcomes."""
    warnings: list[str] = []
    ancestry_problem = (
        exclusion_counts.get("ancestry_unassigned", 0)
        + exclusion_counts.get("ancestry_not_eur", 0)
        + exclusion_counts.get("orientation_failure", 0)
    )
    ancestry_check = "passed" if ancestry_problem == 0 else "passed_with_warnings"
    if ancestry_problem:
        warnings.append(
            f"{ancestry_problem} Analysis/Analyses excluded by ancestry policy "
            "(unassigned, non-target, or orientation failure); see sidecars/exclusions.tsv"
        )

    sd_problem = (
        exclusion_counts.get("sd_no_reference_resource_for_ancestry", 0)
        + exclusion_counts.get("sd_no_qualifying_evidence", 0)
        + exclusion_counts.get("sd_no_usable_sample_size", 0)
        + exclusion_counts.get("sd_failed", 0)
    )
    included_warnings = sum(
        1
        for outcome, row in zip(outcomes, sd_rows)
        if outcome.included and row.get("status") == "warning"
    )
    included_failures = sum(
        1
        for outcome, row in zip(outcomes, sd_rows)
        if outcome.included and row.get("status") == "failed"
    )
    if included_failures:
        sd_check = "failed"
    elif sd_problem or included_warnings:
        sd_check = "passed_with_warnings"
    else:
        sd_check = "passed"
    if included_failures:
        warnings.append(
            f"{included_failures} included Analysis/Analyses have a failed SD estimate; "
            "review sidecars/sd_estimation.tsv before acceptance"
        )
    if sd_problem:
        warnings.append(
            f"{sd_problem} quantitative Analysis/Analyses had no usable phenotype SD "
            "and were excluded; see sidecars/sd_estimation.tsv"
        )
    if included_warnings:
        warnings.append(
            f"{included_warnings} included Analysis/Analyses have a high-dispersion SD "
            "estimate; see sidecars/sd_estimation.tsv"
        )

    resolution_problem = exclusion_counts.get("resolution_failed", 0)
    if resolution_problem:
        warnings.append(
            f"{resolution_problem} Analysis/Analyses failed resolution and were excluded; "
            "see sidecars/exclusions.tsv"
        )
    metadata_problem = exclusion_counts.get("missing_sample_size", 0) + exclusion_counts.get(
        "missing_case_control_counts", 0
    )
    if metadata_problem:
        warnings.append(
            f"{metadata_problem} Analysis/Analyses had incomplete resolved metadata and were "
            "excluded; see sidecars/exclusions.tsv"
        )
    if duplicate_membership:
        groups = sorted(set(duplicate_membership.values()))
        warnings.append(
            f"{len(groups)} duplicate-content group(s) surfaced for review, not collapsed: "
            + "; ".join(groups)
        )
    return ancestry_check, sd_check, sd_check, warnings


# ---------------------------------------------------------------------------
# Bundle documents
# ---------------------------------------------------------------------------


def render_build_yaml(store_id: str, config: CandidateConfiguration) -> str:
    """Render the observed-only Hybrid Build Recipe.

    The recipe is a registry fact plus ``opengwasdb`` flags; it carries no
    artifact root (issue #126) and names a subcommand, never an import path
    (ADR 0023).
    """
    document = {
        "store_id": store_id,
        "layout": config.layout,
        "completion_state": config.completion_state,
        "build": {
            "command": config.build_command,
            "options": dict(config.build_options),
        },
        "post": dict(config.post) or {"top_hits": False, "overview": True},
    }
    return _dump_yaml(document)


def render_release_yaml(
    *,
    store_id: str,
    config: CandidateConfiguration,
    tables: CandidateTables,
    commands: Sequence[str],
    created_at: str,
    inventory_sha256: str,
    preflight_report: Path,
    index_summary: Mapping[str, Any],
    generator_version: str,
) -> str:
    """Render the candidate ``release.yaml`` identity record.

    Only identity, lineage, status, creation time, source-snapshot identity, the
    executed command log and prose survive (ADR 0029). The evidence the candidate
    binds -- the frozen inventory's checksum, the preflight report, the #152
    policy and the executed resolver argv -- is the command log plus prose.
    """
    notes_lines: list[str] = []
    if config.notes.strip():
        notes_lines.append(config.notes.strip())
    notes_lines.append(
        "Candidate status: human review required before acceptance (issue #153). "
        f"{tables.included_rows} included, {tables.excluded_rows} excluded of "
        f"{tables.inventory_rows} frozen inventory rows."
    )
    notes_lines.append(
        "Evidence: frozen Source Inventory "
        f"{config.base.inventory_snapshot_id} (sha256 {inventory_sha256}); "
        f"preflight report {preflight_report}."
    )
    notes_lines.append(
        "Policy: full-reference AF ancestry assignment and source-AF-only quantitative "
        "phenotype-SD estimation (issue #152); case-control rows use log_or/binary_trait; "
        "non-target/unassigned ancestry, orientation failures, unusable source AF and "
        "ordinary resolution failures are controlled exclusions (sidecars/exclusions.tsv)."
    )
    if tables.exclusion_counts:
        notes_lines.append(
            "Exclusions by reason: "
            + ", ".join(f"{reason}={count}" for reason, count in tables.exclusion_counts.items())
        )
    for warning in tables.warnings:
        notes_lines.append(f"Review: {warning}")
    notes = "\n\n".join(notes_lines)

    document = {
        "store_id": store_id,
        "label": config.label,
        "access_posture": config.access_posture,
        "status": CANDIDATE_STATUS,
        "derived_from": None,
        "created_at": created_at,
        "source_snapshot_id": config.base.inventory_snapshot_id,
        "source_snapshot": {
            "inventory_snapshot_id": config.base.inventory_snapshot_id,
            "inventory_tsv_sha256": inventory_sha256,
            "preflight_report": str(preflight_report),
            "resolver_opengwasdb_version": index_summary.get("opengwasdb_version"),
        },
        "generator": {
            "version": generator_version,
            "commands": list(commands),
        },
        "description": config.description,
        "notes": notes,
    }
    return _dump_yaml(document)


def render_validation_yaml(
    *,
    tables: CandidateTables,
    index_summary: Mapping[str, Any],
    validated_at: str,
    validator_name: str,
) -> str:
    """Render the candidate Validation Record.

    No Store was built, so every ``observed`` measurement is ``null``: absence is
    recorded, never guessed (issue #135). The checks describe the Phase B
    evidence that does exist.
    """
    status = "passed_with_warnings" if tables.warnings else "passed"
    document = {
        "status": status,
        "validated_at": validated_at,
        "validator": {"name": validator_name, "version": None},
        "build_environment": {
            "opengwasdb_version": index_summary.get("opengwasdb_version"),
            "opengwasdb_commit": index_summary.get("opengwasdb_git_hash"),
        },
        "observed": {
            "format_version": None,
            "n_analyses": None,
            "n_variants": None,
            "n_associations": None,
            "store_bytes": None,
            "build_elapsed_s": None,
            "validate_status": None,
        },
        "checks": {
            "schema": "passed",
            "files": "passed",
            "ancestry": tables.ancestry_check,
            "effect_scale": tables.effect_scale_check,
            "sd_estimation": tables.sd_check,
        },
        "reports": {
            "source_readiness": "sidecars/source_readiness.tsv",
            "ancestry": "sidecars/ancestry.tsv",
            "sd_estimation": "sidecars/sd_estimation.tsv",
            "exclusions": "sidecars/exclusions.tsv",
        },
        "warnings": list(tables.warnings),
        "errors": [],
    }
    return _dump_yaml(document)


# ---------------------------------------------------------------------------
# Atomic staging and finalisation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateFiles:
    """The complete in-memory candidate bundle, ready to stage."""

    release_yaml: str
    build_yaml: str
    analyses_tsv: str
    validation_yaml: str
    source_readiness_tsv: str
    ancestry_tsv: str
    sd_estimation_tsv: str
    exclusions_tsv: str


def stage_candidate(staging_store_dir: Path, files: CandidateFiles) -> None:
    """Write every bundle file under the staging store directory.

    ``release.yaml`` is written last, so a crash mid-write leaves a staging
    directory that is not a discoverable bundle (``bundle-check``/``index``
    require ``release.yaml``).
    """
    staging_store_dir.mkdir(parents=True, exist_ok=True)
    sidecars = staging_store_dir / "sidecars"
    sidecars.mkdir(parents=True, exist_ok=True)
    (staging_store_dir / "analyses.tsv").write_text(files.analyses_tsv, encoding="utf-8")
    (sidecars / "source_readiness.tsv").write_text(
        files.source_readiness_tsv, encoding="utf-8"
    )
    (sidecars / "ancestry.tsv").write_text(files.ancestry_tsv, encoding="utf-8")
    (sidecars / "sd_estimation.tsv").write_text(files.sd_estimation_tsv, encoding="utf-8")
    (sidecars / "exclusions.tsv").write_text(files.exclusions_tsv, encoding="utf-8")
    (staging_store_dir / "build.yaml").write_text(files.build_yaml, encoding="utf-8")
    (staging_store_dir / "validation.yaml").write_text(files.validation_yaml, encoding="utf-8")
    (staging_store_dir / "release.yaml").write_text(files.release_yaml, encoding="utf-8")


def publish_candidate(registry_root: Path, store_id: str, staging_store_dir: Path) -> Path:
    """Atomically swap a verified staged candidate into ``stores/<store_id>``.

    A failed finalisation must not partially replace a prior candidate, so the
    staged directory is fully written and checked before this is called. The
    swap moves any existing candidate aside first and restores it if the replace
    fails, so the registry always holds either the previous candidate or the new
    one, never a half-written mixture.
    """
    final_dir = registry_root / store_id
    previous_dir = registry_root / STAGING_DIRNAME / f"{store_id}.previous"
    previous_dir.parent.mkdir(parents=True, exist_ok=True)
    if previous_dir.exists():
        shutil.rmtree(previous_dir)
    moved_previous = False
    if final_dir.exists():
        os.replace(final_dir, previous_dir)
        moved_previous = True
    try:
        os.replace(staging_store_dir, final_dir)
    except OSError:
        if moved_previous:
            os.replace(previous_dir, final_dir)
        raise
    if moved_previous:
        shutil.rmtree(previous_dir, ignore_errors=True)
    return final_dir


def cleanup_staging(registry_root: Path) -> None:
    """Remove a leftover staging tree; never touches a published candidate."""
    staging = registry_root / STAGING_DIRNAME
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)


def check_staged_candidate(store_id: str, staging_parent: Path) -> list[str]:
    """Run ``bundle.check()`` against the staged candidate, before publication."""
    from ogstores import bundle as bundle_module

    loaded = bundle_module.load(store_id, registry_root=staging_parent)
    return list(bundle_module.check(loaded, registry_root=staging_parent))


def validate_candidate_analyses(analyses_tsv: str) -> list[str]:
    """Validate the emitted ``analyses.tsv`` against the pinned OpenGWASDB schema.

    ``bundle.check()`` tolerates blank required values for a candidate release;
    this workflow deliberately does not, so a required value left blank on an
    included row fails here instead of being suppressed. Excluded audit rows are
    dropped first, exactly as ADR 0025 drops them at build time.
    """
    import csv as _csv
    import io

    from opengwasdb.model import analyses as opengwasdb_analyses

    reader = _csv.DictReader(io.StringIO(analyses_tsv), delimiter="\t")
    table = opengwasdb_analyses.AnalysesTable(
        fieldnames=tuple(reader.fieldnames or ()),
        rows=tuple(dict(row) for row in reader),
    )
    active = opengwasdb_analyses.AnalysesTable(
        fieldnames=table.fieldnames,
        rows=tuple(
            row for row in table.rows if row.get("exclude_from_build") != "true"
        ),
    )
    return list(opengwasdb_analyses.validate_analyses(active))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes (used for the generator's own identity)."""
    return hashlib.sha256(data).hexdigest()


def _render_tsv(columns: Sequence[str], rows: Iterable[Mapping[str, str]]) -> str:
    lines = ["\t".join(columns)]
    for row in rows:
        values = []
        for name in columns:
            value = row.get(name, "")
            if "\t" in value or "\n" in value or "\r" in value:
                raise CandidateError(
                    f"field {name!r} contains a tab or newline and cannot be one TSV "
                    f"field: {value!r}"
                )
            values.append(value)
        lines.append("\t".join(values))
    return "\n".join(lines) + "\n"


def _dump_yaml(document: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        document, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True
    )


def _float_or_empty(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return ""
    return format_sidecar_float(number)


def _int_or_empty(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return ""


__all__ = [
    "ANCESTRY_SIDECAR_COLUMNS",
    "ANALYSES_COLUMNS",
    "CANDIDATE_STATUS",
    "CandidateConfiguration",
    "CandidateError",
    "CandidateFiles",
    "CandidateMetadata",
    "CandidateTables",
    "EXCLUSION_COLUMNS",
    "EXCLUSION_REASONS",
    "RESOLVER_MANIFEST_COLUMNS",
    "SD_ESTIMATION_SIDECAR_COLUMNS",
    "SOURCE_READINESS_COLUMNS",
    "SUPERPOPULATIONS",
    "AnalysisOutcome",
    "ResolverRow",
    "ResolverRun",
    "apply_release_policy",
    "build_candidate_tables",
    "check_staged_candidate",
    "cleanup_staging",
    "derive_resolver_manifest",
    "load_candidate_configuration",
    "now_utc",
    "publish_candidate",
    "read_candidate_metadata",
    "read_source_label_map",
    "render_build_yaml",
    "render_release_yaml",
    "render_resolver_manifest",
    "render_validation_yaml",
    "resolver_argv",
    "run_resolver",
    "sha256_text",
    "stage_candidate",
    "validate_candidate_analyses",
    "verify_records",
]
