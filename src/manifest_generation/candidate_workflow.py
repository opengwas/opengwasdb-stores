"""The shared Manifest Generation pipeline: frozen Source Inventory -> candidate Release Bundle.

The pipeline every Source Collection uses (ADR 0036), first written for the
GWAS Catalog (issue #153) and generalised onto any release config by #287. It
is the registry-owned orchestration between a frozen Source Inventory, with
its connector files frozen beside it (#283), and a candidate Release Bundle.
It owns five things and nothing else:

- **reading the release config** (``manifest_generation.release_config``) and
  the reviewed per-Analysis listings it names;
- **deriving the canonical resolver manifest** from the frozen inventory's exact
  ``data_file`` paths and the release's per-study-design method tiers, with
  the per-Analysis metadata of the frozen Source Metadata Table;
- **invoking the upstream resolver subprocess** and recording the argv that
  actually ran. The reference panel, the worker pool and per-Analysis
  checkpointing are owned by ``opengwasdb resolve-analyses`` (opengwasdb#208);
  this module never loads a reference, forks a worker, or computes an ancestry,
  allele alignment, or phenotype SD itself;
- **accounting for every record** before anything is written: exactly one record
  per selected Analysis, no missing/stale/duplicate records, and every
  record's source identity and method tier matching the manifest that was
  resolved. Records are kept once per Source Collection and reused by every
  Selection (#284, ``manifest_generation.resolution_area``); the resolution
  receipt names exactly the kept records a candidate is decided from;
- **applying the registry's release membership and exclusion policy** -- the
  decisions in ADR 0025 (``exclude_from_build`` audit rows), issue #152
  (full-reference ancestry assignment; source-AF-only quantitative estimation;
  non-target/unassigned/orientation/SD-unavailable/resolution failures become
  controlled, explained exclusions), the Quality Checks (#285), and the pinned
  OpenGWASDB Analysis schema.

Output is a **candidate-only** Release Bundle, rendered deterministically and
emitted through the shared emitter (``manifest_generation.emit``, #248). It
never builds, validates or materialises a Store, never redownloads or copies a
source body, never invokes Store Release Build, and never infers an accepted
status.

Where a value is the statistics (ancestry fit, allele alignment, phenotype-SD
estimate) it comes from the resolver record; where a value is a release decision
(membership, method tier, exclusion) it is made here. That line is ADR 0012 /
ADR 0017's, not this module's invention.

See ``docs/spec/store-release-workflow.md`` (Manifest Generation) and
``docs/spec/release-config.md``; the operator entry point is
``manifest_generation.generate_candidate``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
import subprocess
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from ogstores.bundle import SUPERPOPULATIONS
from manifest_generation import quality_checks
from manifest_generation.quality_checks import (
    NON_QUANTITATIVE_EFFECT_SCALES,
    CheckSummary,
    Stage,
    diagnostic_count,
    record_section,
)
from manifest_generation.ancestry_sidecar import format_sidecar_float
from manifest_generation.generation_rules import INFO_SCORE_KINDS
from manifest_generation.indexed_subsets import resolve_indexed_subsets
from manifest_generation.resolution_area import (
    KeptRecord,
    ResolutionArea,
    ResolutionAreaError,
)
from manifest_generation.safe_write import atomic_write_bytes
from manifest_generation.source_inventory import (
    INVENTORY_COLUMNS,
    SourceInventoryRow,
    duplicate_content_groups,
)
from manifest_generation.release_config import ReleaseConfig, load_release_config
from manifest_generation.connector_tsv import FreezeError
from manifest_generation.freeze import load_frozen_connectors
from manifest_generation.trait_ontology_mapping import (
    CanonicalTraitMappingTable,
    SourceTrait,
    TraitOntologyMapping,
    TraitOntologyMappingError,
    TraitOntologyMappingRun,
    map_trait_ontologies,
)

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

#: The only Release Status a Phase B candidate workflow may write.
CANDIDATE_STATUS: str = "candidate"

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
    "info_score_threshold",
    "imputation_score_column",
    "imputation_score_kind",
    "imputation_score_provenance",
    "maf_threshold",
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
    "source_reader_capability",
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
    "info_score_threshold",
    "imputation_score_column",
    "imputation_score_kind",
    "imputation_score_provenance",
    "maf_threshold",
    "inclusion_reason",
    "exclude_from_build",
)

#: ``sidecars/source_readiness.tsv``: the frozen inventory's 14 columns plus the
#: two derived columns that make it a membership audit rather than a copy.
SOURCE_READINESS_COLUMNS: tuple[str, ...] = INVENTORY_COLUMNS + (
    "duplicate_content_group",
    "candidate_membership",
)

#: ``sidecars/reference_overlap.tsv`` (issue #174). The ``rows_scanned`` /
#: ``variant_reference_rows_matched`` / ``variant_reference_rate`` columns are
#: the legacy pre-INFO counts kept for older records; the
#: ``build_eligible_*`` columns are the post-INFO denominator this gate uses
#: whenever a record carries it.
REFERENCE_OVERLAP_COLUMNS: tuple[str, ...] = (
    "analysis_id", "ancestry_reference_id", "ancestry_rows_scanned",
    "ancestry_rows_matched", "ancestry_reference_rate", "variant_reference",
    "rows_scanned", "variant_reference_rows_matched", "variant_reference_rate",
    "build_eligible_rows", "build_eligible_rows_on_variant_reference",
    "build_eligible_rows_off_variant_reference", "build_eligible_rate",
    "stop_reason", "exclude_from_build",
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

# Issue #174: stop before a Hybrid build projects OGS-00011-scale overflow;
# <5% matched rows signals a likely source-specific assembly/allele mismatch.
MAX_OFF_REFERENCE_SHARE = 0.25
LOW_OVERLAP_RATE = 0.05

#: The two resolver values that mean a declared score was actually evaluated:
#: `disabled` is an explicit zero floor, `filtered` a positive one. A declared
#: Analysis whose record reports anything else has no usable score evidence.
USABLE_INFO_SCORE_STATES: frozenset[str] = frozenset({"disabled", "filtered"})

#: The two resolver ``maf_state`` values that mean the threshold was actually
#: applied: `disabled` is an explicit zero floor, `filtered` a positive one. Any
#: other value (including the ``unavailable`` of an absent request) is no MAF
#: evidence, so ``analyses.tsv`` emits literal ``NaN``.
USABLE_MAF_STATES: frozenset[str] = frozenset({"disabled", "filtered"})

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

#: Exclusion reasons that are release policy rather than a Quality Check (#285),
#: with their sidecar ``category``: a missing or failed Resolution Record has
#: nothing to measure, missing counts are Source Metadata Table facts, and the
#: Store Composition Rule is a release-level admission rule (ADR 0033). Every
#: other reason is declared by a Quality Check (``quality_checks``).
POLICY_EXCLUSION_CATEGORIES: Mapping[str, str] = {
    "resolution_failed": "resolution",
    "missing_sample_size": "metadata",
    "missing_case_control_counts": "metadata",
    "not_maf_filtered": "store_composition",
}


def controlled_exclusion_categories() -> dict[str, str]:
    """The controlled exclusion vocabulary (#152), each reason with its category.

    Every excluded ready Analysis carries exactly one of these, so "why is this
    Analysis absent" has one spelling: the release policy's own reasons and
    those of every registered Quality Check.
    """
    return {**POLICY_EXCLUSION_CATEGORIES, **quality_checks.exclusion_categories()}


#: Resolver record statuses that this module treats as a completed resolution.
_RESOLVER_RECORD_STATUSES: tuple[str, ...] = ("success", "controlled_failure")

#: The resolver record/index schema this registry can finalise. A record or index
#: declaring a different version is incompatible and must fail rather than be
#: interpreted field-by-field against a schema it was not written for.
SUPPORTED_RECORD_SCHEMA_VERSION: int = 1

#: The resolution-receipt schema this module writes and reads. Version 2 (#284)
#: names each Analysis's kept Resolution Record in the Source Collection's
#: resolution area -- fingerprint digest, record checksum and path.
RECEIPT_SCHEMA_VERSION: int = 2

#: Every key a resolution receipt carries. A receipt with any other key is
#: refused; a later release-policy key (#285) is added here explicitly.
RECEIPT_KEYS: frozenset[str] = frozenset(
    {
        "receipt_schema_version",
        "record_schema_version",
        "manifest_sha256",
        "analysis_ids",
        "resolver",
        "contract",
        "resolution_area",
        "records",
    }
)

#: Receipt versions written before #284, whose records lived in the Store
#: Release's Work Root (``resolver/records/``). Receipts of emitted candidates
#: (``source_snapshot.resolver_receipt`` in their ``release.yaml``) remain their
#: provenance; reading one again is refused by name rather than as corrupt.
HISTORICAL_RECEIPT_SCHEMA_VERSIONS: tuple[int, ...] = (1,)

#: Filename of the registry-side resolution receipt in the run's Work Root.
RECEIPT_FILENAME: str = "resolution_receipt.json"


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
    """A release config reduced to the facts candidate generation needs (#287).

    ``base`` is the loaded release config (``manifest_generation.release_config``):
    the Source Collection and its frozen inventory, the Selection, method
    tiers, Reference Resources and runtime, which preflight reads too, so a
    candidate and its preflight cannot disagree about them. The fields here
    are the release identity, the Build Recipe shape and the release policy in
    the form this module applies them, with the reviewed listings read.
    """

    base: ReleaseConfig
    label: str
    access_posture: str
    description: str
    notes: str
    #: The collection-wide Analytical Metadata defaults (``defaults``): an
    #: Analysis whose Source Metadata Table cell is blank takes the default.
    defaults: Mapping[str, Any]
    layout: str
    completion_state: str
    build_command: str
    build_options: Mapping[str, Any]
    post: Mapping[str, Any]
    #: ``maf_floor``, ``extraction_panel`` and ``gates``, as the resolver is passed them.
    ancestry_block: Mapping[str, Any]
    #: ``maf_min``, ``maf_max`` and ``dispersion_max``, where declared.
    effect_scale_block: Mapping[str, Any]
    reader_capability: str
    #: The super-population the Selection expects (``release_policy.selection_superpopulation``).
    target_ancestry: str
    info_score_threshold: str
    imputation_score_declarations: Path | None
    #: Requested per-Analysis MAF floor, canonical string, or ``None`` when
    #: ``defaults.maf_threshold`` is omitted (= no MAF filter at all).
    maf_threshold: str | None
    #: Genotyping technologies whose Analyses are exempt from the default MAF
    #: floor (``release_policy.maf_filter_exempt_genotyping_technologies``). An
    #: Analysis is exempt when it has non-empty technology metadata and every
    #: technology is in this list.
    maf_filter_exempt_genotyping_technologies: tuple[str, ...]
    #: The reviewed per-Analysis MAF-floor exemption file
    #: (``release_policy.maf_filter_exempt_analyses``), or ``None`` when the key
    #: is absent (= no Analysis is exempt).
    maf_filter_exempt_analyses_path: Path | None
    #: ``analysis_id -> recorded reason`` read from that file. An exempt
    #: Analysis emits literal ``NaN`` exactly as an exempt technology does, and
    #: the reason is carried into the resolution receipt.
    maf_filter_exempt_analyses: Mapping[str, str]
    #: The reviewed effect-placeholder exclusion file
    #: (``release_policy.effect_placeholder_exclusions``), or ``None`` when absent.
    effect_placeholder_exclusions_path: Path | None = None
    #: ``analysis_id -> recorded reason`` read from that file; each listed
    #: Analysis is excluded as ``effect_placeholder_rows``.
    effect_placeholder_exclusions: Mapping[str, str] = field(default_factory=dict)
    #: ``release_policy.store_composition.require_maf_filtered`` (issue #203): a
    #: shared Store admits only Analyses whose rows were MAF-filtered, so an
    #: included Analysis with no applied MAF floor is excluded as
    #: ``not_maf_filtered``.
    require_maf_filtered: bool = False
    #: The ``indexed_subsets`` declarations to emit (stores #210). ``None`` means
    #: emit no key at all (a Ragged release); the default is the maintained
    #: ``hm3`` declaration for Dense and Hybrid releases.
    indexed_subsets: tuple[Mapping[str, Any], ...] | None = None
    #: The reviewed reason an operator deliberately omitted the default (`#210`).
    #: Recorded in release.yaml notes so the omission is reviewable.
    indexed_subsets_opt_out: str | None = None
    #: The release's Quality Check threshold overrides (``quality_checks:``,
    #: #285): check name -> parsed threshold. Empty means every check uses its
    #: shared default. Recorded in the resolution receipt and validation.yaml.
    quality_check_overrides: Mapping[str, Any] = field(default_factory=dict)


#: The reader a release uses when its Build Recipe names none.
DEFAULT_SOURCE_READER_CAPABILITY: str = "opengwasdb.gwas-ssf"


def load_candidate_configuration(path: Path, repo_root: Path) -> CandidateConfiguration:
    """Load the release config (``release_config``) and read the reviewed listings it names.

    The schema is the release config's: an unknown key, a missing required
    fact or a legacy Selection is refused there (#287). What is added here is
    only what candidate generation derives from it -- the resolver's reader
    capability and blocks, and the reviewed per-Analysis listings' contents.
    """
    base = load_release_config(path, repo_root)
    return CandidateConfiguration(
        base=base,
        label=base.label,
        access_posture=base.access_posture,
        description=base.description,
        notes=base.notes,
        defaults=dict(base.defaults),
        layout=base.build.layout,
        completion_state=base.build.completion_state,
        build_command=base.build.command,
        build_options=dict(base.build.options),
        post=dict(base.build.post),
        ancestry_block={
            "maf_floor": base.ancestry_maf_floor,
            "extraction_panel": base.ancestry_extraction_panel,
            "gates": dict(base.ancestry_gates),
        },
        effect_scale_block=dict(base.effect_scale),
        reader_capability=str(
            base.build.options.get("source-reader-capability") or DEFAULT_SOURCE_READER_CAPABILITY
        ),
        target_ancestry=base.selection_superpopulation,
        info_score_threshold=base.info_score_threshold,
        imputation_score_declarations=base.imputation_score_declarations,
        maf_threshold=base.maf_threshold,
        maf_filter_exempt_genotyping_technologies=base.maf_filter_exempt_genotyping_technologies,
        maf_filter_exempt_analyses_path=base.maf_filter_exempt_analyses_path,
        maf_filter_exempt_analyses=read_maf_filter_exempt_analyses(
            base.maf_filter_exempt_analyses_path
        ),
        effect_placeholder_exclusions_path=base.effect_placeholder_exclusions_path,
        effect_placeholder_exclusions=read_effect_placeholder_exclusions(
            base.effect_placeholder_exclusions_path
        ),
        require_maf_filtered=base.require_maf_filtered,
        indexed_subsets=base.indexed_subsets,
        indexed_subsets_opt_out=base.indexed_subsets_opt_out,
        quality_check_overrides=dict(base.quality_check_overrides),
    )


#: The exact reviewed MAF-floor exemption table header
#: (``release_policy.maf_filter_exempt_analyses``); see ``analysis_maf_threshold``.
MAF_FILTER_EXEMPTION_COLUMNS: tuple[str, ...] = ("analysis_id", "reason")


def read_maf_filter_exempt_analyses(path: Path | None) -> dict[str, str]:
    """Read the reviewed per-Analysis MAF-floor exemptions, or ``{}`` when absent.

    The table is the operator's explicit, auditable record of *why* an Analysis
    carries no MAF floor -- for example an ``effect_allele_frequency`` column
    that is ``0.0`` on every row, which core reads as a missing frequency. The
    reason is kept so the exemption is a reviewed fact rather than an implicit
    ``NaN``. A duplicate or blank ``analysis_id``, a blank ``reason``, a
    different header, or an unreadable file is an error (never a skipped row).
    """
    return _read_reviewed_analysis_table(path, "MAF filter exemption")


#: The exact reviewed effect-placeholder exclusion table header
#: (``release_policy.effect_placeholder_exclusions``); same shape as the MAF exemptions.
EFFECT_PLACEHOLDER_EXCLUSION_COLUMNS: tuple[str, ...] = MAF_FILTER_EXEMPTION_COLUMNS


def read_effect_placeholder_exclusions(path: Path | None) -> dict[str, str]:
    """Read the reviewed per-Analysis effect-placeholder exclusions, or ``{}``.

    Some GWAS-SSF sources write an effect they did not estimate as
    ``±2.2250738585072014e-308`` (the smallest normal double: only the sign
    survives) with a ``standard_error`` of 0. Core derives a missing standard
    error from the effect and the p-value (opengwasdb#236) and only refuses an
    effect of exactly 0, so such a row is stored as an effect of ~0 with a
    standard error of ~1e-308. Until core refuses the placeholder itself, an
    Analysis carrying any such row is listed here and excluded as
    ``effect_placeholder_rows``, with the measured row counts as its reason.
    Validation is the MAF exemption table's: exact header, no blank or
    duplicate ``analysis_id``, non-empty ``reason``.
    """
    return _read_reviewed_analysis_table(path, "effect placeholder exclusion")


def _read_reviewed_analysis_table(path: Path | None, what: str) -> dict[str, str]:
    """``analysis_id -> reason`` from a reviewed two-column TSV, or ``{}`` when absent."""
    if path is None:
        return {}
    if not path.is_file():
        raise CandidateError(f"{what} table not found: {path}")
    found: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if reader.fieldnames != list(MAF_FILTER_EXEMPTION_COLUMNS):
            raise CandidateError(
                f"{path}: expected exact TSV headers {MAF_FILTER_EXEMPTION_COLUMNS!r}"
            )
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise CandidateError(f"{path}:{number}: malformed {what} row")
            analysis_id = row["analysis_id"].strip()
            reason = row["reason"].strip()
            if not analysis_id:
                raise CandidateError(f"{path}:{number}: analysis_id must be non-empty")
            if not reason:
                raise CandidateError(f"{path}:{number}: reason must be non-empty")
            if analysis_id in found:
                raise CandidateError(f"{path}:{number}: duplicate analysis_id {analysis_id!r}")
            found[analysis_id] = reason
    return found


def info_score_emission(
    declaration: ResolverRow | None, record: Mapping[str, Any] | None
) -> tuple[str, str, str, str]:
    """The four ``analyses.tsv`` INFO cells for one Analysis (stores #175).

    A numeric floor is emitted only on per-Analysis evidence, never on a
    capability-wide allowlist: the Analysis must carry an approved declaration,
    its resolver record must report a usable ``info_score_state``
    (``disabled``/``filtered``) with at least one usable score, and its
    fingerprint must still bind that exact declaration. Every other Analysis --
    undeclared, a legacy record without the new diagnostics, or a declared one
    whose evidence is unusable -- emits literal ``NaN`` and empty triple cells.
    A declared Analysis with zero usable scores (record state
    ``no_usable_scores``) is *included* and emits ``NaN``, not excluded (#176).
    """
    if declaration is None or not declaration.imputation_score_column:
        return ("NaN", "", "", "")
    diagnostics = record_section(record, "diagnostics")
    if diagnostics.get("info_score_state") not in USABLE_INFO_SCORE_STATES:
        return ("NaN", "", "", "")
    usable = diagnostics.get("info_rows_usable")
    if not _positive_int(usable):
        return ("NaN", "", "", "")
    resolution_config = record_section(
        record_section(record, "fingerprints"), "resolution_config"
    )
    if resolution_config.get("info_score_threshold") != float(declaration.info_score_threshold):
        return ("NaN", "", "", "")
    for key in SCORE_DECLARATION_COLUMNS[1:]:
        if resolution_config.get(key) != getattr(declaration, key):
            return ("NaN", "", "", "")
    return (
        declaration.info_score_threshold,
        declaration.imputation_score_column,
        declaration.imputation_score_kind,
        declaration.imputation_score_provenance,
    )


def _positive_int(value: Any) -> bool:
    """True only for a real, strictly positive ``int`` (a bool is not a count)."""
    return type(value) is int and value > 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def maf_threshold_emission(
    declaration: ResolverRow | None, record: Mapping[str, Any] | None
) -> str:
    """The ``analyses.tsv`` MAF cell for one Analysis (stores #176).

    A numeric floor is emitted only on per-Analysis resolver evidence: the
    Analysis's manifest row must carry a numeric ``maf_threshold``, its resolver
    record must report a usable ``maf_state`` (``disabled``/``filtered``), and
    its fingerprint must still bind that exact value. Every other Analysis --
    no configured default, an exempt genotyping technology, a legacy record
    without the diagnostics, or a changed fingerprint -- emits literal ``NaN``.
    """
    if declaration is None or declaration.maf_threshold in ("", "NaN"):
        return "NaN"
    diagnostics = record_section(record, "diagnostics")
    if diagnostics.get("maf_state") not in USABLE_MAF_STATES:
        return "NaN"
    resolution_config = record_section(
        record_section(record, "fingerprints"), "resolution_config"
    )
    try:
        wanted = float(declaration.maf_threshold)
    except ValueError:
        return "NaN"
    if resolution_config.get("maf_threshold") != wanted:
        return "NaN"
    return declaration.maf_threshold


def read_genotyping_technologies(yaml_file: str) -> list[str]:
    """Read a source metadata YAML's ``genotyping_technology`` list, or ``[]``.

    Missing metadata is not an exemption: a row whose sidecar is absent,
    unreadable, or carries no technology falls through to the release default.
    The inventory records the exact YAML path in its ``yaml_file`` column.
    """
    if not yaml_file:
        return []
    path = Path(yaml_file)
    if not path.is_file():
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(document, Mapping):
        return []
    raw = document.get("genotyping_technology")
    if isinstance(raw, str):
        values: list[Any] = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def analysis_maf_threshold(row: SourceInventoryRow, config: CandidateConfiguration) -> str:
    """The manifest ``maf_threshold`` for one Analysis: the default or ``NaN``.

    An Analysis is exempt when ``release_policy.maf_filter_exempt_analyses`` lists it
    (the operator's reviewed reason is carried into the resolution receipt), or
    when it has non-empty technology metadata and *every* technology is in
    ``release_policy.maf_filter_exempt_genotyping_technologies``. Missing technology
    metadata is never an exemption (#176).
    """
    if config.maf_threshold is None:
        return "NaN"
    if row.analysis_id in config.maf_filter_exempt_analyses:
        return "NaN"
    exempt = set(config.maf_filter_exempt_genotyping_technologies)
    if exempt:
        technologies = read_genotyping_technologies(row.yaml_file)
        if technologies and all(technology in exempt for technology in technologies):
            return "NaN"
    return config.maf_threshold


# ---------------------------------------------------------------------------
# Per-Analysis source metadata (the frozen Source Metadata Table)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisMetadata:
    """One Analysis's source metadata, from the frozen Source Metadata Table (#283, #287).

    The frozen Source Inventory records selection identity, the exact source
    path and readiness; the per-Analysis metadata is the Source Metadata
    Table's, frozen beside the inventory and checksummed into its provenance
    (``freeze.load_frozen_connectors``). Reading it is a join on an already
    frozen Selection, not a second selection: membership is the inventory's.
    A blank cell means the source supplied no value; the release config's
    ``defaults`` fill the collection-wide ones when rows are rendered.

    The source's own ontology term is carried verbatim. Its Trait Ontology
    Mapping is the shared step's (#286, ADR 0036 step 5), run after
    verification by :func:`map_candidate_trait_ontologies`; until then
    ``trait_ontology`` is ``None`` and a bundle row cannot be rendered.
    """

    analysis_id: str
    #: The Source Trait Label (``source_trait_label``).
    source_label: str
    #: ``source_trait_ontology_id``, verbatim: a CURIE, a URI or a comma-separated list.
    source_trait_ontology_id: str
    #: ``source_trait_ontology_label``, verbatim.
    source_trait_ontology_label: str
    publication_pmid: str
    first_author: str
    n_cases: str
    n_controls: str
    sample_size: str
    #: The Source Ancestry Label, verbatim provenance; never Assigned Ancestry.
    source_ancestry_label: str = ""
    #: The super-population the source declares; never Assigned Ancestry.
    source_superpopulation: str = ""
    publication_doi: str = ""
    consortium: str = ""
    license: str = ""
    source_genome_build: str = ""
    trait_ontology: TraitOntologyMapping | None = None


def read_analysis_metadata(
    provenance_path: Path, analysis_ids: Iterable[str]
) -> dict[str, AnalysisMetadata]:
    """Read ``analysis_ids``' rows of the Source Metadata Table frozen with the inventory.

    The frozen table is checked against the sha256 its provenance records
    before it is read. A selected ready Analysis with no row cannot have
    schema-valid metadata, so it fails loudly rather than being emitted with a
    guessed label or a fabricated case count.
    """
    try:
        table, _manifest = load_frozen_connectors(provenance_path)
    except FreezeError as exc:
        raise CandidateError(
            f"cannot read the frozen Source Metadata Table: {exc}. Freeze the Source "
            "Inventory with the shared freeze (pixi run inventory-freeze, or the "
            "pipeline's freeze stage) so its connectors are frozen beside it"
        ) from exc
    wanted = sorted(set(analysis_ids))
    missing = [analysis_id for analysis_id in wanted if analysis_id not in table]
    if missing:
        raise CandidateError(
            f"the frozen Source Metadata Table {table.path} has no row for {len(missing)} "
            f"selected ready Analysis/Analyses: {', '.join(missing[:10])}"
        )
    found: dict[str, AnalysisMetadata] = {}
    for analysis_id in wanted:
        row = table.row(analysis_id)
        found[analysis_id] = AnalysisMetadata(
            analysis_id=analysis_id,
            source_label=row.get("source_trait_label", ""),
            source_trait_ontology_id=row.get("source_trait_ontology_id", ""),
            source_trait_ontology_label=row.get("source_trait_ontology_label", ""),
            publication_pmid=row.get("publication_pmid", ""),
            first_author=row.get("first_author", ""),
            n_cases=row.get("n_cases", ""),
            n_controls=row.get("n_controls", ""),
            sample_size=row.get("sample_size", ""),
            source_ancestry_label=row.get("source_ancestry_label", ""),
            source_superpopulation=row.get("source_superpopulation", ""),
            publication_doi=row.get("publication_doi", ""),
            consortium=row.get("consortium", ""),
            license=row.get("license", ""),
            source_genome_build=row.get("source_genome_build", ""),
        )
    return found


def trait_sources(metadata: Mapping[str, AnalysisMetadata]) -> list[SourceTrait]:
    """The Trait Ontology Mapping step's input: each Analysis's source trait columns."""
    return [
        SourceTrait(
            analysis_id=analysis_id,
            label=resolved.source_label,
            ontology_id=resolved.source_trait_ontology_id,
            ontology_label=resolved.source_trait_ontology_label,
        )
        for analysis_id, resolved in sorted(metadata.items())
    ]


def map_candidate_trait_ontologies(
    metadata: Mapping[str, AnalysisMetadata],
    table: CanonicalTraitMappingTable,
) -> tuple[dict[str, AnalysisMetadata], TraitOntologyMappingRun]:
    """Run the shared Trait Ontology Mapping step over every Analysis (#286).

    Returns the metadata with each Analysis's ``trait_ontology`` set, and the
    run, whose provenance validation.yaml records. A source term in a form the
    step cannot normalise fails the run, naming the Analysis and the term.
    """
    try:
        run = map_trait_ontologies(trait_sources(metadata), table)
    except TraitOntologyMappingError as exc:
        raise CandidateError(f"Trait Ontology Mapping failed: {exc}") from exc
    mapped = {
        analysis_id: replace(resolved, trait_ontology=run.mappings[analysis_id])
        for analysis_id, resolved in metadata.items()
    }
    return mapped, run


# ---------------------------------------------------------------------------
# Canonical resolver manifest
# ---------------------------------------------------------------------------


SCORE_DECLARATION_COLUMNS: tuple[str, ...] = (
    "analysis_id", "imputation_score_column", "imputation_score_kind",
    "imputation_score_provenance",
)


def read_imputation_score_declarations(
    path: Path | None, analysis_ids: Iterable[str]
) -> dict[str, tuple[str, str, str]]:
    """Read reviewed per-Analysis provider citations, never source-header guesses.

    A citation is required but its scientific independence requires human review.
    """
    if path is None:
        return {}
    if not path.is_file():
        raise CandidateError(f"imputation score declaration table not found: {path}")
    known = set(analysis_ids)
    found: dict[str, tuple[str, str, str]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if reader.fieldnames != list(SCORE_DECLARATION_COLUMNS):
            raise CandidateError(
                f"{path}: expected exact TSV headers {SCORE_DECLARATION_COLUMNS!r}"
            )
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise CandidateError(f"{path}:{number}: malformed declaration row")
            analysis_id = row["analysis_id"].strip()
            if not analysis_id or analysis_id not in known:
                raise CandidateError(f"{path}:{number}: unknown analysis_id {analysis_id!r}")
            if analysis_id in found:
                raise CandidateError(f"{path}:{number}: duplicate analysis_id {analysis_id!r}")
            column, kind, provenance = (
                row[name] for name in SCORE_DECLARATION_COLUMNS[1:]
            )
            if not column or column != column.strip() or any(c in column for c in "\r\n\t"):
                raise CandidateError(
                    f"{path}:{number}: imputation_score_column must be an exact "
                    "non-empty header name"
                )
            if kind not in INFO_SCORE_KINDS:
                raise CandidateError(f"{path}:{number}: invalid imputation_score_kind {kind!r}")
            if not provenance or not provenance.strip() or any(c in provenance for c in "\r\n\t"):
                raise CandidateError(
                    f"{path}:{number}: imputation_score_provenance requires "
                    "independent provider evidence"
                )
            found[analysis_id] = (column, kind, provenance)
    return found


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
    info_score_threshold: str = "NaN"
    imputation_score_column: str = ""
    imputation_score_kind: str = ""
    imputation_score_provenance: str = ""
    #: The per-Analysis MAF floor: the configured default, or literal ``NaN``
    #: for a no-MAF-filter release or a MAF-exempt genotyping technology.
    maf_threshold: str = "NaN"

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in RESOLVER_MANIFEST_COLUMNS}


def derive_resolver_manifest(
    rows: Sequence[SourceInventoryRow],
    config: CandidateConfiguration,
    metadata: Mapping[str, AnalysisMetadata],
) -> list[ResolverRow]:
    """Derive the canonical resolver manifest from the frozen exact paths.

    Every ready inventory row becomes exactly one manifest row. The method tier
    comes from ``defaults.by_study_design`` (a design with no tier is a
    preflight failure, so it cannot reach here), the source identity comes from
    the inventory's recorded ``data_file``/``sha256``/``data_bytes`` -- never
    reconstructed from an accession -- and the total N comes from the resolved
    metadata. ``data_file`` is used verbatim; nothing here opens it.
    """
    declarations = read_imputation_score_declarations(
        config.imputation_score_declarations, (row.analysis_id for row in rows)
    )
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
        column, kind, provenance = declarations.get(row.analysis_id, ("", "", ""))
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
                info_score_threshold=config.info_score_threshold if column else "NaN",
                imputation_score_column=column,
                imputation_score_kind=kind,
                imputation_score_provenance=provenance,
                maf_threshold=analysis_maf_threshold(row, config),
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


def resolver_settings(config: CandidateConfiguration) -> list[str]:
    """The ``opengwasdb resolve-analyses`` flags that apply to every Analysis.

    Only registry facts and declared config values are composed: the declared
    ancestry reference and its fine-group map, the extraction panel, the
    Variant Reference axis, reference-AF resources and the declared gates and
    floors. Everything else is the resolver's own default. Per ADR 0023 the
    registry names a subcommand and passes flags through, and does not mirror a
    parameter schema. The manifest, the records directory and the worker count
    are a run's, not settings, and :func:`resolver_argv` adds them.
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
    settings: list[str] = [
        "--ancestry-reference",
        ancestry_resource.location,
        "--ancestry-groups",
        fine_group_map,
    ]
    extraction_panel = config.ancestry_block.get("extraction_panel")
    if extraction_panel:
        settings.extend(["--extraction-panel", str(extraction_panel)])
    axis = config.build_options.get("variant-reference")
    if axis:
        settings.extend(["--variant-reference", str(axis)])
    for spec in _resolve_af_reference_specs(config):
        settings.extend(["--af-reference", spec])
    settings.extend(
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
        ]
    )
    return settings


def resolver_argv(
    *,
    resolver_bin: str,
    manifest_path: Path,
    records_dir: Path,
    config: CandidateConfiguration,
    cores: int,
) -> list[str]:
    """Compose the exact ``opengwasdb resolve-analyses`` argv.

    The resolver always resumes: it reuses every record whose fingerprint still
    matches and re-resolves the rest (#284).
    """
    return [
        resolver_bin,
        RESOLVER_SUBCOMMAND,
        str(manifest_path),
        str(records_dir),
        *resolver_settings(config),
        "--n-workers",
        str(cores),
        "--resume",
    ]


#: Resolver flags whose value is a file the resolver reads, so the settings
#: digest binds the file's content and not only its path.
_RESOLVER_FILE_FLAGS: tuple[str, ...] = (
    "--ancestry-reference",
    "--ancestry-groups",
    "--extraction-panel",
    "--variant-reference",
)


def resolver_settings_digest(settings: Sequence[str]) -> str:
    """The digest that routes a run to its checkpoint in the Resolution Area (#284).

    It covers the resolver settings that enter every record's fingerprint:
    :func:`resolver_settings` as passed, the content checksum of every file
    they name, and the installed resolver's identity. It is only a routing key
    -- the resolver checks each Analysis's full fingerprint on ``--resume`` --
    so a setting it misses causes a re-resolve, never a wrong reuse.
    """
    files: dict[str, str] = {}
    for position, token in enumerate(settings[:-1]):
        value = settings[position + 1]
        if token in _RESOLVER_FILE_FLAGS:
            path = value
        elif token == "--af-reference":
            path = value.partition("=")[2]
        else:
            continue
        if not Path(path).is_file():
            raise CandidateError(
                f"resolver setting {token} names {path}, which is not a file; refusing "
                "to resolve against it"
            )
        files[path] = sha256_file(Path(path))
    version, resolver_sha256 = _opengwasdb_tool_identity()
    payload = {
        "settings": list(settings),
        "file_sha256": files,
        "opengwasdb_version": version,
        "opengwasdb_resolver_sha256": resolver_sha256,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Resolver execution and record accounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolverRun:
    """What one resolver invocation did, for the evidence log."""

    argv: list[str]
    returncode: int
    log_path: Path


def run_resolver(
    argv: Sequence[str], *, cwd: Path, log_path: Path, pass_fds: Sequence[int] = ()
) -> ResolverRun:
    """Run the resolver subprocess, writing its output to a durable log.

    The registry does not parse the resolver's stdout for accounting: the
    accounting comes from the records and ``index.json``, which are the
    resolver's declared contract. The raw output goes straight to the log, so
    a failure is auditable without re-running it, and the resolver never
    writes into a pipe that dies with this process. ``pass_fds`` hands it
    descriptors to hold, such as the Resolution Area's lock.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            pass_fds=tuple(pass_fds),
            check=False,
        )
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


def account_records(
    manifest_rows: Sequence[ResolverRow],
    records_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Account one resolver run's output against the manifest it resolved.

    ``records_dir`` is the directory the resolver wrote: its ``index.json`` must
    describe exactly this manifest, and it must hold one record per manifest row.
    Returns ``(records, failures)`` where ``records`` is in manifest order and
    ``failures`` lists every way the records are missing, duplicated or
    incompatible with the pinned record schema. This is the resolver's own
    contract, checked without reference to the resolution receipt.

    The directory is a Source Collection's checkpoint, shared by every Selection
    (#284), so a record there for an Analysis outside this manifest belongs to
    another Selection and is not a failure. What a candidate was decided from is
    what its resolution receipt names, and :func:`verify_records` refuses a
    receipt that names any record outside the manifest.
    """
    failures = _duplicate_manifest_ids(manifest_rows)
    index_failures, index_usable = _account_index(manifest_rows, records_dir / "index.json")
    failures.extend(index_failures)
    if not index_usable:
        return [], failures
    records: list[dict[str, Any]] = []
    for row in manifest_rows:
        record, row_failures = _account_record(row, records_dir / f"{row.analysis_id}.json")
        failures.extend(row_failures)
        if record is not None:
            records.append(record)
    return records, failures


def verify_records(
    manifest_rows: Sequence[ResolverRow],
    *,
    config: CandidateConfiguration,
    manifest_path: Path,
    receipt_path: Path,
    index_path: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Account the records a receipt names *and* prove they are current.

    The resolution receipt names, per Analysis, the kept Resolution Record the
    candidate is decided from: its fingerprint digest and its checksum in the
    Source Collection's Resolution Area (#284). This reads exactly those
    records, requires each to be byte-for-byte the record the receipt bound, and
    accounts each against its manifest row as :func:`account_records` does for
    a fresh run. ``index_path`` is the run's copy of the resolver's index.

    A record's own fingerprint digest only proves it is internally consistent.
    A record set produced under different gates, ancestry reference/group map,
    extraction panel, reference-AF resources or tool revision is still
    internally consistent, so this function also recomputes the current
    resolution contract and requires the receipt to match it. Any failure means
    the candidate is not emitted: a partial, stale or extra-contract record set
    is exactly the silently-wrong bundle this repository exists to prevent.
    """
    failures = _duplicate_manifest_ids(manifest_rows)
    failures.extend(_account_index(manifest_rows, index_path)[0])
    try:
        receipt = read_resolution_receipt(receipt_path)
        area = ResolutionArea.from_receipt(receipt["resolution_area"])
    except (CandidateError, ResolutionAreaError) as exc:
        return [], failures + [str(exc)]
    kept, parse_failures = parse_receipt_records(manifest_rows, receipt["records"])
    failures.extend(parse_failures)
    records, record_failures = account_kept_records(manifest_rows, area, kept)
    failures.extend(record_failures)
    failures.extend(
        _verify_receipt(
            manifest_rows=manifest_rows,
            records=records,
            kept=kept,
            receipt=receipt,
            config=config,
            manifest_path=manifest_path,
            receipt_path=receipt_path,
        )
    )
    return records, failures


def parse_receipt_records(
    manifest_rows: Sequence[ResolverRow], named: Any
) -> tuple[dict[str, KeptRecord], list[str]]:
    """Parse a receipt's ``records`` into one :class:`KeptRecord` per Analysis."""
    if not isinstance(named, Mapping):
        return {}, ["the resolution receipt's records is not a mapping"]
    failures: list[str] = []
    expected = {row.analysis_id for row in manifest_rows}
    outside = [str(analysis_id) for analysis_id in named if analysis_id not in expected]
    if outside:
        failures.append(
            f"the resolution receipt names {len(outside)} record(s) for Analyses outside "
            f"this Selection's manifest: {', '.join(outside[:10])}"
        )
    kept: dict[str, KeptRecord] = {}
    for row in manifest_rows:
        if row.analysis_id not in named:
            failures.append(
                f"{row.analysis_id}: the resolution receipt names no Resolution Record for "
                "this Analysis; re-run resolution"
            )
            continue
        try:
            kept[row.analysis_id] = KeptRecord.from_receipt_entry(
                row.analysis_id, named[row.analysis_id]
            )
        except ResolutionAreaError as exc:
            failures.append(str(exc))
    return kept, failures


def account_kept_records(
    manifest_rows: Sequence[ResolverRow],
    area: ResolutionArea,
    kept: Mapping[str, KeptRecord],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read and account the kept Resolution Records a receipt names, in manifest order."""
    failures: list[str] = []
    records: list[dict[str, Any]] = []
    for row in manifest_rows:
        record_ref = kept.get(row.analysis_id)
        if record_ref is None:
            continue  # parse_receipt_records already reported this.
        try:
            data = area.read_kept(record_ref)
        except ResolutionAreaError as exc:
            failures.append(str(exc))
            continue
        record, row_failures = _account_record(row, area.record_path(record_ref), data)
        failures.extend(row_failures)
        if record is not None:
            records.append(record)
    return records, failures


def _duplicate_manifest_ids(manifest_rows: Sequence[ResolverRow]) -> list[str]:
    expected_ids = [row.analysis_id for row in manifest_rows]
    duplicate_ids = sorted({aid for aid in expected_ids if expected_ids.count(aid) > 1})
    if duplicate_ids:
        return [f"resolver manifest has duplicate analysis_id: {', '.join(duplicate_ids)}"]
    return []


def _account_index(
    manifest_rows: Sequence[ResolverRow], index_path: Path
) -> tuple[list[str], bool]:
    """Check a resolver ``index.json`` describes exactly this manifest.

    Returns ``(failures, usable)``; ``usable`` is false when the index is
    missing or unreadable, so there is nothing to account against.
    """
    failures: list[str] = []
    expected_ids = [row.analysis_id for row in manifest_rows]
    if not index_path.is_file():
        return [f"resolver index is missing: {index_path}"], False
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"resolver index {index_path} is not valid JSON: {exc}"], False
    if not isinstance(index, Mapping):
        return [f"resolver index {index_path} is not a JSON mapping"], False
    index_schema_version = index.get("record_schema_version")
    if index_schema_version != SUPPORTED_RECORD_SCHEMA_VERSION:
        failures.append(
            f"resolver index {index_path} declares record_schema_version "
            f"{index_schema_version!r}; this registry accepts only "
            f"{SUPPORTED_RECORD_SCHEMA_VERSION}"
        )

    recorded = index.get("analyses")
    if not isinstance(recorded, list):
        failures.append(f"resolver index {index_path} has no 'analyses' list")
        return failures, False
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
    return failures, True


def _account_record(
    row: ResolverRow, record_path: Path, data: bytes | None = None
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read one resolver record and check it against its manifest row.

    ``data`` is the record's bytes when the caller has already read them.
    Returns the record, or ``None`` when it cannot be used at all.
    """
    if data is None:
        if not record_path.is_file():
            return None, [f"{row.analysis_id}: resolver record is missing ({record_path})"]
        data = record_path.read_bytes()
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{row.analysis_id}: resolver record is not valid JSON: {exc}"]
    if not isinstance(record, Mapping):
        return None, [f"{row.analysis_id}: resolver record is not a JSON mapping"]
    record_schema_version = record.get("record_schema_version")
    if record_schema_version != SUPPORTED_RECORD_SCHEMA_VERSION:
        return None, [
            f"{row.analysis_id}: resolver record declares record_schema_version "
            f"{record_schema_version!r}; this registry accepts only "
            f"{SUPPORTED_RECORD_SCHEMA_VERSION}"
        ]
    if record.get("analysis_id") != row.analysis_id:
        return None, [
            f"{row.analysis_id}: resolver record names analysis_id "
            f"{record.get('analysis_id')!r}"
        ]
    status = record.get("status")
    if status not in _RESOLVER_RECORD_STATUSES:
        return None, [f"{row.analysis_id}: resolver record has unexpected status {status!r}"]
    return dict(record), _verify_fingerprints(row, record)


def _first_mismatch(actual: Sequence[str], expected: Sequence[str]) -> str:
    for left, right in zip(actual, expected):
        if left != right:
            return f"{left!r} != {right!r}"
    return f"length {len(actual)} != {len(expected)}"


def _verify_fingerprints(row: ResolverRow, record: Mapping[str, Any]) -> list[str]:
    """Check a record's current source identity and tier against the manifest row.

    A record whose recorded source checksum, size, file, tier or reader
    capability no longer matches the manifest was produced against a different
    input than the one being finalised. With ``--resume`` the resolver re-runs
    such records; if one survives to here it is stale, and a stale record must
    fail finalisation rather than be frozen into a candidate. The resolver's
    fingerprint also records the source file's current size and mtime, so a
    source replaced after the resolve is caught here too.
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
    # A declared score must be included in the resolver's own fingerprint.
    # Legacy/no-declaration records predate these keys and remain compatible.
    if row.imputation_score_column:
        if isinstance(resolution_config, Mapping):
            for key in ("info_score_threshold",) + SCORE_DECLARATION_COLUMNS[1:]:
                wanted = (
                    float(row.info_score_threshold)
                    if key == "info_score_threshold" else getattr(row, key)
                )
                if resolution_config.get(key) != wanted:
                    failures.append(
                        f"{row.analysis_id}: resolver resolution_config.{key} is "
                        f"{resolution_config.get(key)!r}, expected {wanted!r}"
                    )
    # A numeric MAF request must be bound by the resolver's own fingerprint, so
    # a record resolved without MAF filtering cannot be frozen as if it had it.
    # A no-MAF-filter (literal NaN) row binds no key, as for an undeclared score.
    if row.maf_threshold not in ("", "NaN") and isinstance(resolution_config, Mapping):
        wanted_maf = float(row.maf_threshold)
        if resolution_config.get("maf_threshold") != wanted_maf:
            failures.append(
                f"{row.analysis_id}: resolver resolution_config.maf_threshold is "
                f"{resolution_config.get('maf_threshold')!r}, expected {wanted_maf!r}"
            )
    failures.extend(_verify_source_file_unchanged(row, fingerprints))
    return failures


def _verify_source_file_unchanged(
    row: ResolverRow, fingerprints: Mapping[str, Any]
) -> list[str]:
    """Require the source file to still match the size/mtime the resolver recorded."""
    try:
        stat = Path(row.source_file).stat()
    except OSError:
        return [
            f"{row.analysis_id}: source file for the resolver record cannot be "
            f"inspected: {row.source_file}"
        ]
    failures: list[str] = []
    recorded_bytes = fingerprints.get("source_file_bytes")
    if recorded_bytes != stat.st_size:
        failures.append(
            f"{row.analysis_id}: source file size is {stat.st_size} but the resolver "
            f"record was produced against {recorded_bytes!r}; re-run resolution"
        )
    recorded_mtime = fingerprints.get("source_file_mtime_ns")
    if recorded_mtime != stat.st_mtime_ns:
        failures.append(
            f"{row.analysis_id}: source file mtime is {stat.st_mtime_ns} but the "
            f"resolver record was produced against {recorded_mtime!r}; re-run resolution"
        )
    return failures


# ---------------------------------------------------------------------------
# Resolution receipt
# ---------------------------------------------------------------------------
#
# The hole the receipt closes: a record's own fingerprint digest only says the
# record is *internally* consistent. A record produced under different gates, a
# different ancestry reference or fine-group map, a different extraction panel,
# different reference-AF resources, a different evidence bound or a different
# tool revision is still internally consistent, so a standalone verify/emit that
# only recomputed that self-digest would freeze a stale Analysis into a
# candidate. After every successful resolver invocation the registry therefore
# persists a resolution receipt: the resolver manifest's checksum, the
# registry-recomputable slice of the resolution contract, the tool identity the
# resolver reported, and every analysis_id bound to the kept Resolution Record
# it was decided from -- fingerprint digest and record checksum, in the Source
# Collection's resolution area (#284). Standalone verify/emit recompute the
# contract from the current config, references and installed tool and require
# it to equal the receipt, and read exactly the records it names; they never
# re-derive the resolver's statistics.


def sha256_file(path: Path) -> str:
    """SHA-256 hex digest of a file, streamed so a genome-scale reference fits.

    One run hashes each reference for its settings digest, the contract it
    binds and the contract it verifies, so a digest is remembered for the
    process, keyed by the file's identity and modification time.
    """
    stat = Path(path).stat()
    key = (str(Path(path).resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cached = _SHA256_BY_FILE_IDENTITY.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    _SHA256_BY_FILE_IDENTITY[key] = digest.hexdigest()
    return _SHA256_BY_FILE_IDENTITY[key]


_SHA256_BY_FILE_IDENTITY: dict[tuple[str, int, int, int, int], str] = {}


def _opengwasdb_tool_identity() -> tuple[str, str | None]:
    """The installed OpenGWASDB version and resolver-module content fingerprint.

    The distribution version is read from installed metadata; the resolver
    implementation's identity is the SHA-256 of
    ``opengwasdb/build/resolve_manifest.py``. The module content, not the
    enclosing repository's ``HEAD``, is the meaningful tool identity here: a
    wheel installed inside this repository would otherwise report this
    repository's revision as ``git rev-parse HEAD`` does, so a commit of an
    unrelated registry file would spuriously invalidate every receipt. A pin
    bump, or an in-place edit of the resolver, changes the module digest and
    makes the receipt stale, which is what the receipt must detect.
    """
    version = ""
    try:
        from importlib.metadata import version as distribution_version

        version = distribution_version("opengwasdb")
    except Exception:
        version = ""
    resolver_sha256: str | None = None
    try:
        from opengwasdb.build import resolve_manifest

        resolver_sha256 = sha256_file(Path(resolve_manifest.__file__))
    except Exception:
        resolver_sha256 = None
    return version, resolver_sha256


def compute_resolution_contract(config: CandidateConfiguration) -> dict[str, Any]:
    """The registry-recomputable slice of what a resolution run depends on.

    Every value here is a registry fact or a content fingerprint of a declared
    Reference Resource -- never a statistic. ``verify_records`` recomputes this
    from the current config and files and requires it to equal the receipt, so a
    changed gate, ancestry reference, fine-group map, extraction panel, or
    reference-AF resource invalidates the records that were resolved against the
    old one.
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
    extraction_panel = config.ancestry_block.get("extraction_panel")
    extraction_panel_path = str(extraction_panel) if extraction_panel else None

    af_references: list[dict[str, Any]] = []
    for ancestry, resource_id in config.base.effect_scale_reference_resources:
        resource = config.base.reference_resources.get(resource_id)
        if resource is None or not resource.location:
            continue
        af_references.append(
            {
                "ancestry": ancestry,
                "resource_id": resource_id,
                "location": resource.location,
                "sha256": sha256_file(Path(resource.location)),
            }
        )

    opengwasdb_version, opengwasdb_resolver_sha256 = _opengwasdb_tool_identity()
    contract = {
        "source_reader_capability": config.reader_capability,
        "maf_floor": config.ancestry_block.get("maf_floor", 0.01),
        "gates": {
            "tau": gates.get("tau", 0.50),
            "delta": gates.get("delta", 0.20),
            "n_min": gates.get("n_min", 5000),
            "residual_max": gates.get("residual_max", 0.06),
        },
        "extraction_panel": extraction_panel_path,
        "extraction_panel_sha256": (
            sha256_file(Path(extraction_panel_path)) if extraction_panel_path else None
        ),
        "ancestry_reference": ancestry_resource.location,
        "ancestry_reference_sha256": sha256_file(Path(ancestry_resource.location)),
        "ancestry_groups": fine_group_map,
        "ancestry_groups_sha256": sha256_file(Path(fine_group_map)),
        "af_references": af_references,
        "opengwasdb_version": opengwasdb_version,
        "opengwasdb_resolver_sha256": opengwasdb_resolver_sha256,
    }
    # Keep the legacy no-mapping contract byte-for-byte compatible. A newly
    # configured mapping changes both the manifest and this receipt contract.
    if config.imputation_score_declarations is not None:
        contract["imputation_score_declarations"] = str(config.imputation_score_declarations)
        contract["imputation_score_declarations_sha256"] = sha256_file(
            config.imputation_score_declarations
        )
    # A configured MAF floor is part of the resolution contract: changing the
    # default or the exemption rule re-derives every manifest row's
    # maf_threshold, so a receipt resolved under the old rule must be rejected
    # rather than silently frozen. A no-MAF (omitted) config adds no keys, so the
    # legacy contract stays byte-for-byte compatible.
    if config.maf_threshold is not None:
        contract["maf_threshold"] = config.maf_threshold
        contract["maf_filter_exempt_genotyping_technologies"] = list(
            config.maf_filter_exempt_genotyping_technologies
        )
        # The reviewed per-Analysis exemptions and their recorded reasons are
        # named here too, so the rationale for a ``NaN`` floor is auditable in
        # the run's own receipt and not only in the committed table.
        if config.maf_filter_exempt_analyses_path is not None:
            contract["maf_filter_exempt_analyses"] = str(
                config.maf_filter_exempt_analyses_path
            )
            contract["maf_filter_exempt_analyses_reasons"] = dict(
                config.maf_filter_exempt_analyses
            )
    # The effective Quality Check thresholds, defaults included, and the
    # release's overrides (#285): changing a shared default or an override makes
    # the receipt stale. A receipt written before #285 lacks the key and is
    # stale too. Re-running the resolve stage rewrites it, reusing every record:
    # thresholds are not in the resolver's fingerprint (#284).
    contract["quality_checks"] = quality_checks.receipt_contract(config.quality_check_overrides)
    return contract


def build_resolution_receipt(
    *,
    manifest_rows: Sequence[ResolverRow],
    config: CandidateConfiguration,
    manifest_path: Path,
    area: ResolutionArea,
    kept: Mapping[str, KeptRecord],
    argv: Sequence[str],
    settings_digest: str,
    index: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a successful resolver run to its contract and the records it kept.

    ``records`` names, in manifest order, each Analysis's kept Resolution Record
    in the Source Collection's resolution area: its fingerprint digest, its
    checksum and its path in the area (#284). It names exactly this Selection's
    records, whether this run resolved them or reused them. ``settings_digest``
    names the checkpoint the run resolved into.
    """
    contract = compute_resolution_contract(config)
    index_version = index.get("opengwasdb_version")
    index_git_hash = index.get("opengwasdb_git_hash")
    if not isinstance(index_version, str) or not isinstance(index_git_hash, str):
        raise CandidateError(
            "resolver index does not report its opengwasdb_version and "
            "opengwasdb_git_hash; refusing to write a resolution receipt"
        )
    if index_version != contract["opengwasdb_version"]:
        raise CandidateError(
            "resolver index reports opengwasdb version "
            f"{index_version!r} but the installed opengwasdb is "
            f"{contract['opengwasdb_version']!r}; refusing to write a resolution receipt"
        )
    records: dict[str, dict[str, str]] = {}
    for row in manifest_rows:
        record = kept.get(row.analysis_id)
        if record is None:
            raise CandidateError(f"{row.analysis_id}: no Resolution Record was kept for the receipt")
        records[row.analysis_id] = record.as_receipt_entry()
    return {
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "record_schema_version": SUPPORTED_RECORD_SCHEMA_VERSION,
        "manifest_sha256": sha256_file(manifest_path),
        "analysis_ids": [row.analysis_id for row in manifest_rows],
        "resolver": {
            "argv": list(argv),
            "settings_digest": settings_digest,
            "opengwasdb_version": index_version,
            "opengwasdb_git_hash": index_git_hash,
        },
        "contract": contract,
        "resolution_area": str(area.root),
        "records": records,
    }


def write_resolution_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Write the receipt atomically and durably, replacing an earlier run's."""
    atomic_write_bytes(
        path,
        (json.dumps(receipt, indent=2, sort_keys=False) + "\n").encode("utf-8"),
        path.parent,
        overwrite=True,
    )


def read_resolution_receipt(path: Path) -> dict[str, Any]:
    """Read and version-check a resolution receipt, or raise :class:`CandidateError`."""
    if not path.is_file():
        raise CandidateError(
            f"resolution receipt is missing: {path}; the records must be produced by the "
            "workflow's resolve stage, not hand-assembled"
        )
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CandidateError(f"resolution receipt {path} is not valid JSON: {exc}") from exc
    if not isinstance(receipt, Mapping):
        raise CandidateError(f"resolution receipt {path} is not a JSON mapping")
    receipt_schema_version = receipt.get("receipt_schema_version")
    if receipt_schema_version in HISTORICAL_RECEIPT_SCHEMA_VERSIONS:
        raise CandidateError(
            f"resolution receipt {path} has receipt_schema_version "
            f"{receipt_schema_version!r}: a historical receipt, whose records lived in "
            "the Store Release's own Work Root before Resolution Records were kept per "
            "Source Collection (#284). It stays the provenance of the candidate it "
            "produced but cannot be verified again; re-run the resolve stage"
        )
    if receipt_schema_version != RECEIPT_SCHEMA_VERSION:
        raise CandidateError(
            f"resolution receipt {path} has incompatible receipt_schema_version "
            f"{receipt_schema_version!r}; this registry writes and reads "
            f"{RECEIPT_SCHEMA_VERSION}"
        )
    record_schema_version = receipt.get("record_schema_version")
    if record_schema_version != SUPPORTED_RECORD_SCHEMA_VERSION:
        raise CandidateError(
            f"resolution receipt {path} has incompatible record_schema_version "
            f"{record_schema_version!r}; this registry finalises "
            f"{SUPPORTED_RECORD_SCHEMA_VERSION}"
        )
    missing = sorted(RECEIPT_KEYS - set(receipt))
    if missing:
        raise CandidateError(f"resolution receipt {path} is missing key(s) {missing}")
    unknown = sorted(set(receipt) - RECEIPT_KEYS)
    if unknown:
        raise CandidateError(
            f"resolution receipt {path} has unknown key(s) {unknown}; this registry "
            f"reads only {sorted(RECEIPT_KEYS)}"
        )
    return dict(receipt)


def _verify_receipt(
    *,
    manifest_rows: Sequence[ResolverRow],
    records: Sequence[Mapping[str, Any]],
    kept: Mapping[str, KeptRecord],
    receipt: Mapping[str, Any],
    config: CandidateConfiguration,
    manifest_path: Path,
    receipt_path: Path,
) -> list[str]:
    """Require the current contract and every record digest to match the receipt.

    This is the deep, registry-side staleness check. It never recomputes the
    resolver's fingerprint math; it recomputes the *inputs* that determine it,
    compares them to the receipt the successful run wrote, and requires each
    record to still carry the exact digest that run bound.
    """
    failures: list[str] = []
    try:
        current_contract = compute_resolution_contract(config)
    except (CandidateError, OSError) as exc:
        return [
            "current resolution contract cannot be recomputed, so the resolver "
            f"records cannot be proven current: {exc}"
        ]

    receipt_contract = receipt.get("contract")
    if not isinstance(receipt_contract, Mapping):
        failures.append(f"resolution receipt {receipt_path} has no contract mapping")
    else:
        failures.extend(_contract_differences(receipt_contract, current_contract))

    current_manifest_sha = sha256_file(manifest_path)
    if receipt.get("manifest_sha256") != current_manifest_sha:
        failures.append(
            "the resolver manifest changed since the successful resolve; resolution "
            f"must be re-run (receipt {receipt.get('manifest_sha256')!r}, current "
            f"{current_manifest_sha!r})"
        )

    expected_ids = [row.analysis_id for row in manifest_rows]
    receipt_ids = receipt.get("analysis_ids")
    if receipt_ids != expected_ids:
        failures.append(
            "the resolution receipt accounts a different Analysis set than the current "
            f"manifest ({receipt_ids!r} != {expected_ids!r})"
        )

    by_id = {record.get("analysis_id"): record for record in records}
    for row in manifest_rows:
        record = by_id.get(row.analysis_id)
        record_ref = kept.get(row.analysis_id)
        if record is None or record_ref is None:
            continue  # parse_receipt_records or account_kept_records reported this.
        fingerprints = record.get("fingerprints")
        digest = (
            fingerprints.get("fingerprint_digest") if isinstance(fingerprints, Mapping) else None
        )
        expected_digest = record_ref.fingerprint_digest
        if digest != expected_digest:
            failures.append(
                f"{row.analysis_id}: resolver fingerprint digest is {digest!r} but the "
                f"successful resolution bound {expected_digest!r}; the record is stale or "
                "was produced by a different run"
            )
    return failures


def _contract_differences(
    receipt_contract: Mapping[str, Any], current_contract: Mapping[str, Any]
) -> list[str]:
    """Name every field of the resolution contract that changed since the receipt."""
    failures: list[str] = []
    for key in sorted(set(receipt_contract) | set(current_contract)):
        before = receipt_contract.get(key)
        after = current_contract.get(key)
        if before != after:
            failures.append(
                f"resolution contract changed since the successful resolve: {key} "
                f"was {before!r}, now {after!r}; resolution must be re-run"
            )
    return failures


# ---------------------------------------------------------------------------
# Release membership and exclusion policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisOutcome:
    """One selected ready Analysis after the release policy was applied."""

    row: SourceInventoryRow
    metadata: AnalysisMetadata
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
    #: The emitted INFO columns (stores #175). Default is the no-evidence pair:
    #: literal ``NaN`` and empty triple cells. (:func:`info_score_emission`).
    info_score_threshold: str = "NaN"
    info_score_column: str = ""
    info_score_kind: str = ""
    info_score_provenance: str = ""
    #: The emitted MAF floor (stores #176): the numeric value on resolver
    #: evidence, else literal ``NaN``. (:func:`maf_threshold_emission`).
    maf_threshold: str = "NaN"
    #: Every registered Quality Check's result for this Analysis (#285), in
    #: registry order, whether or not one of them decided its membership.
    check_results: tuple[quality_checks.Finding, ...] = ()

    @property
    def analysis_id(self) -> str:
        return self.row.analysis_id

    @property
    def maf_filtered(self) -> bool:
        """True when a MAF floor was emitted on resolver evidence (#203)."""
        return self.maf_threshold != "NaN"


def apply_release_policy(
    rows: Sequence[SourceInventoryRow],
    config: CandidateConfiguration,
    metadata: Mapping[str, AnalysisMetadata],
    records: Mapping[str, Mapping[str, Any]],
    manifest_rows: Sequence[ResolverRow] = (),
) -> list[AnalysisOutcome]:
    """Decide membership for every selected ready Analysis, with a reason.

    The order matters and encodes the #152 decision: an Analysis that cannot be
    given a trustworthy Assigned Ancestry, or whose effects cannot be put on the
    release's declared scale, becomes an ``exclude_from_build`` audit row with
    one controlled reason -- never a silently dropped row and never a fabricated
    value. Duplicate-content accessions are not collapsed: each keeps its own
    row and its own decision.

    Every registered Quality Check (``quality_checks``, #285) is evaluated for
    every Analysis and its result kept on the outcome. A failed exclusion check
    excludes at its declared stage; a warning check never changes membership.

    The Store-composition gate (#203) is the *last* one, on purpose: when
    ``store_composition.require_maf_filtered`` is on, an otherwise-included
    Analysis whose emitted ``maf_threshold`` is literal ``NaN`` is excluded as
    ``not_maf_filtered``, but an Analysis that already failed any earlier gate
    keeps that reason. ``not_maf_filtered`` therefore means "admissible except
    for Store composition".

    ``manifest_rows`` are the resolver manifest rows this run resolved, if the
    caller has them: they carry the approved per-Analysis INFO declarations that
    decide the emitted threshold (stores #175). Omitting them emits ``NaN`` for
    every Analysis, the no-declaration behaviour, so the Store-composition gate
    refuses to run without them rather than exclude every Analysis.
    """
    if config.require_maf_filtered and not manifest_rows:
        raise CandidateError(
            "store_composition.require_maf_filtered needs the resolver manifest rows: "
            "without them no Analysis can show an applied MAF floor, and every one "
            "would be excluded as not_maf_filtered"
        )
    declaration_by_id = {row.analysis_id: row for row in manifest_rows}
    outcomes: list[AnalysisOutcome] = []
    for row in rows:
        if not row.ready:
            continue
        try:
            outcome = _decide(
                row=row,
                resolved=metadata[row.analysis_id],
                tier=config.base.method_tiers[row.study_design],
                record=records.get(row.analysis_id),
                config=config,
                declaration=declaration_by_id.get(row.analysis_id),
            )
        except quality_checks.QualityCheckError as exc:
            raise CandidateError(str(exc)) from exc
        if config.require_maf_filtered and outcome.included and not outcome.maf_filtered:
            outcome = replace(
                outcome,
                included=False,
                exclusion_reason="not_maf_filtered",
                exclusion_detail=_not_maf_filtered_detail(
                    declaration_by_id.get(row.analysis_id),
                    records.get(row.analysis_id),
                    row,
                ),
            )
        outcomes.append(outcome)
    return outcomes


def _not_maf_filtered_detail(
    declaration: ResolverRow | None,
    record: Mapping[str, Any] | None,
    row: SourceInventoryRow,
) -> str:
    """The ``not_maf_filtered`` audit detail (issue #203).

    Names the requested floor, the resolver's own MAF state, the Analysis's
    genotyping technologies, and the off-reference share of its build-eligible
    rows, so the exclusion can be judged from the sidecar alone. Absence is
    rendered explicitly (``undeclared``, ``unrecorded``, ``?``), never as a zero
    count or as a declared ``NaN``.
    """
    diagnostics = record_section(record, "diagnostics")
    declared = declaration.maf_threshold if declaration is not None else "undeclared"
    state = str(diagnostics.get("maf_state") or "unrecorded")
    technologies = "; ".join(read_genotyping_technologies(row.yaml_file)) or "unrecorded"
    off = diagnostic_count(diagnostics.get("build_eligible_rows_off_variant_reference"))
    eligible = diagnostic_count(diagnostics.get("build_eligible_rows"))
    return (
        f"no MAF floor applied (requested maf_threshold={declared}, "
        f"resolver maf_state={state}, genotyping_technology={technologies}); "
        f"{off} of {eligible} build-eligible rows are off the variant reference (#203)"
    )


def _decide(
    *,
    row: SourceInventoryRow,
    resolved: AnalysisMetadata,
    tier: Any,
    record: Mapping[str, Any] | None,
    config: CandidateConfiguration,
    declaration: ResolverRow | None = None,
) -> AnalysisOutcome:
    """One Analysis's membership: release policy, with Quality Checks in its slots.

    The order is the #152 decision. Each ``Stage`` is a slot where the first
    failed exclusion check at that stage decides; a new stage needs a slot here.
    """
    stored_effect_scale = tier.stored_effect_scale
    case_control = stored_effect_scale in NON_QUANTITATIVE_EFFECT_SCALES
    sample_size = resolved.sample_size
    n_cases = resolved.n_cases if case_control else ""
    n_controls = resolved.n_controls if case_control else ""
    emitted_info = info_score_emission(declaration, record)
    emitted_maf = maf_threshold_emission(declaration, record)
    # Every check measures every Analysis, so the sidecar records each result
    # whatever decides membership below.
    findings = quality_checks.evaluate(
        quality_checks.Subject(
            analysis_id=row.analysis_id,
            record=record,
            stored_effect_scale=stored_effect_scale,
            selection_ancestry=config.target_ancestry,
            effect_placeholder_reason=config.effect_placeholder_exclusions.get(row.analysis_id),
        ),
        config.quality_check_overrides,
    )

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
            info_score_threshold=emitted_info[0],
            info_score_column=emitted_info[1],
            info_score_kind=emitted_info[2],
            info_score_provenance=emitted_info[3],
            maf_threshold=emitted_maf,
            check_results=findings,
        )

    def excluded_by(finding: quality_checks.Finding, **carried: str) -> AnalysisOutcome:
        return make(included=False, reason=finding.reason, detail=finding.detail, **carried)

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

    # Slot RESOLVED: checks on the record before its Assigned Ancestry is read
    # (build-eligible rows, the reviewed effect-placeholder listing).
    failed = quality_checks.first_exclusion(findings, Stage.RESOLVED)
    if failed is not None:
        return excluded_by(failed)

    # A declared score with no usable evidence is *included*: every row is
    # retained and this Analysis emits literal ``NaN`` INFO cells (#176). The
    # record reports ``info_score_state = "no_usable_scores"``, which
    # :func:`info_score_emission` reads as no evidence; it is never a controlled
    # failure and never a refusal here.

    assigned = str(record_section(record, "ancestry").get("assigned_ancestry") or "").strip()

    # Slot ANCESTRY_READ: EAF orientation. The audit row keeps the value read.
    failed = quality_checks.first_exclusion(findings, Stage.ANCESTRY_READ)
    if failed is not None:
        return excluded_by(failed, assigned=assigned)

    # Slot SELECTION: the Assigned Ancestry check (ADR 0036). An assignment that
    # is a super-population code is carried as af_assigned, even when it is
    # not the Selection's.
    failed = quality_checks.first_exclusion(findings, Stage.SELECTION)
    if failed is not None:
        if assigned in SUPERPOPULATIONS:
            return excluded_by(failed, assigned=assigned, method="af_assigned")
        return excluded_by(failed)

    # From here the Analysis has the Selection's ancestry; the only remaining
    # question is whether its effects are on the release's scale.
    if case_control and (not _positive(n_cases) or not _positive(n_controls)):
        return make(
            included=False,
            reason="missing_case_control_counts",
            detail=f"n_cases={n_cases or 'empty'} n_controls={n_controls or 'empty'}",
            assigned=assigned,
            method="af_assigned",
        )

    # Slot TARGET_ANCESTRY: the phenotype SD.
    failed = quality_checks.first_exclusion(findings, Stage.TARGET_ANCESTRY)
    if failed is not None:
        return excluded_by(failed, assigned=assigned, method="af_assigned")

    # Nothing excluded it, so every exclusion check must have tested it.
    quality_checks.require_measured(findings, row.analysis_id)
    if case_control:
        return make(
            included=True,
            assigned=assigned,
            method="af_assigned",
            original_sd="",
            sd_method="binary_trait",
        )
    # The phenotype_sd check passed, so this is a positive finite estimate.
    estimate = record_section(record_section(record, "phenotype_sd"), "estimate")
    return make(
        included=True,
        assigned=assigned,
        method="af_assigned",
        original_sd=format_sidecar_float(float(estimate["sd"])),
        sd_method=str(estimate.get("method") or tier.original_sd_method),
    )


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
    reference_overlap_tsv: str
    reference_overlap: Mapping[str, Any]
    reference_overlap_errors: tuple[str, ...]
    store_composition: Mapping[str, Any]
    inventory_rows: int
    included_rows: int
    excluded_rows: int
    exclusion_counts: Mapping[str, int]
    ancestry_check: str
    sd_check: str
    effect_scale_check: str
    warnings: tuple[str, ...]
    #: ``sidecars/quality_checks.tsv``: one row per selected Analysis (#285).
    quality_checks_tsv: str = ""
    #: Each applied Quality Check's summary, keyed by name (#285).
    quality_checks: Mapping[str, CheckSummary] = field(default_factory=dict)


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
    overlap_rows, overlap_summary, overlap_errors = _reference_overlap(outcomes, config)

    exclusion_counts: dict[str, int] = {}
    for outcome in outcomes:
        if not outcome.included:
            exclusion_counts[outcome.exclusion_reason] = (
                exclusion_counts.get(outcome.exclusion_reason, 0) + 1
            )
    unknown = sorted(set(exclusion_counts) - set(controlled_exclusion_categories()))
    if unknown:
        raise CandidateError(f"internal error: uncontrolled exclusion reason(s): {unknown}")

    ancestry_check, sd_check, effect_scale_check, warnings = _derive_checks(
        outcomes, sd_rows, exclusion_counts, duplicate_membership
    )
    store_composition = _store_composition(outcomes, config)
    warnings.extend(_store_composition_warnings(store_composition))
    quality_checks_tsv, quality_check_summary = _quality_check_tables(outcomes, config)
    warnings.extend(quality_checks.warning_lines(quality_check_summary))

    return CandidateTables(
        analyses_tsv=_render_tsv(ANALYSES_COLUMNS, analyses_rows),
        source_readiness_tsv=_render_tsv(SOURCE_READINESS_COLUMNS, readiness_rows),
        ancestry_tsv=_render_tsv(ANCESTRY_SIDECAR_COLUMNS, ancestry_rows),
        sd_estimation_tsv=_render_tsv(SD_ESTIMATION_SIDECAR_COLUMNS, sd_rows),
        exclusions_tsv=_render_tsv(EXCLUSION_COLUMNS, exclusion_rows),
        reference_overlap_tsv=_render_tsv(REFERENCE_OVERLAP_COLUMNS, overlap_rows),
        reference_overlap=overlap_summary,
        reference_overlap_errors=tuple(overlap_errors),
        store_composition=store_composition,
        inventory_rows=len(inventory_rows),
        included_rows=sum(1 for outcome in outcomes if outcome.included),
        excluded_rows=sum(1 for outcome in outcomes if not outcome.included),
        exclusion_counts=dict(sorted(exclusion_counts.items())),
        ancestry_check=ancestry_check,
        sd_check=sd_check,
        effect_scale_check=effect_scale_check,
        warnings=tuple(warnings),
        quality_checks_tsv=quality_checks_tsv,
        quality_checks=quality_check_summary,
    )


def _quality_check_tables(
    outcomes: Sequence[AnalysisOutcome], config: CandidateConfiguration
) -> tuple[str, dict[str, CheckSummary]]:
    """The Quality Check sidecar and its ``validation.yaml`` summary (#285).

    The columns come from the checks the policy applied, which every outcome
    carries in the same order; with no outcomes they are the registry's.
    """
    declared = (
        quality_checks.declared_checks(outcomes[0].check_results)
        if outcomes
        else quality_checks.checks()
    )
    for outcome in outcomes:
        if quality_checks.declared_checks(outcome.check_results) != declared:
            raise CandidateError(
                f"internal error: {outcome.analysis_id} carries Quality Check results "
                f"for a different check set than {[check.name for check in declared]}"
            )
    rows = [
        quality_checks.sidecar_row(
            outcome.analysis_id,
            outcome.included,
            outcome.exclusion_reason,
            outcome.check_results,
        )
        for outcome in outcomes
    ]
    summary = quality_checks.summarise(
        [
            quality_checks.Decided(
                outcome.included, outcome.exclusion_reason, outcome.check_results
            )
            for outcome in outcomes
        ],
        config.quality_check_overrides,
    )
    return _render_tsv(quality_checks.sidecar_columns(declared), rows), summary


def _store_composition(
    outcomes: Sequence[AnalysisOutcome], config: CandidateConfiguration
) -> dict[str, Any]:
    """Summarise MAF-filtered admission for the shared Store (issue #203).

    ``unfiltered_*`` counts the Analyses whose emitted ``maf_threshold`` is
    literal ``NaN`` -- those included despite no applied MAF floor, and those
    routed out by the ``not_maf_filtered`` gate. The off-reference row sums use
    :func:`_off_reference_sum`, which never treats absence as zero.
    """
    unfiltered_included = [
        outcome for outcome in outcomes if outcome.included and not outcome.maf_filtered
    ]
    routed = [
        outcome
        for outcome in outcomes
        if not outcome.included and outcome.exclusion_reason == "not_maf_filtered"
    ]
    return {
        "require_maf_filtered": config.require_maf_filtered,
        "maf_floor": config.maf_threshold,
        "unfiltered_included": len(unfiltered_included),
        "unfiltered_included_off_reference_rows": _off_reference_sum(
            unfiltered_included, config
        ),
        "unfiltered_routed": len(routed),
        "unfiltered_routed_off_reference_rows": _off_reference_sum(routed, config),
    }


def _off_reference_sum(
    outcomes: Sequence[AnalysisOutcome], config: CandidateConfiguration
) -> int | None:
    """Sum off-reference build-eligible rows, or ``None`` when it is not knowable.

    The config must declare a ``build.options.variant-reference`` axis and every
    counted Analysis must carry a valid non-negative integer
    ``build_eligible_rows_off_variant_reference``; absence is never summed as
    zero. With zero counted Analyses and a declared axis the sum is a definite
    ``0``.
    """
    if not config.build_options.get("variant-reference"):
        return None
    total = 0
    for outcome in outcomes:
        diagnostics = record_section(outcome.record, "diagnostics")
        off = diagnostics.get("build_eligible_rows_off_variant_reference")
        if type(off) is not int or off < 0:
            return None
        total += off
    return total


def _store_composition_warnings(composition: Mapping[str, Any]) -> list[str]:
    """The Store-composition review warnings (issue #203), from its summary."""
    warnings: list[str] = []
    routed = composition["unfiltered_routed"]
    if routed:
        warnings.append(
            f"{routed} Analysis/Analyses had no MAF floor applied and were excluded "
            "from this shared Store as not_maf_filtered (#203); see "
            "sidecars/exclusions.tsv"
        )
    unfiltered_included = composition["unfiltered_included"]
    if (
        not composition["require_maf_filtered"]
        and composition["maf_floor"] is not None
        and unfiltered_included
    ):
        warnings.append(
            f"{unfiltered_included} included Analysis/Analyses had no MAF floor "
            "applied, so their off-reference rows enter the shared variant axis "
            "unfiltered (#203); see store_composition in validation.yaml"
        )
    return warnings


def _reference_overlap(
    outcomes: Sequence[AnalysisOutcome], config: CandidateConfiguration
) -> tuple[list[dict[str, str]], dict[str, Any], list[str]]:
    """Summarise the post-INFO reference overlap that decides projected overflow.

    The denominator is an Analysis's **build-eligible** rows after any declared
    INFO filter (`build_eligible_rows`, stores #175) whenever its resolver record
    carries those diagnostics; only a record without them falls back to the
    legacy pre-INFO ``rows_read`` / ``variant_reference_rows_matched`` counts. A
    zero build-eligible denominator is unavailable, never a fabricated zero
    off-axis share. The ancestry scan can stop before the physical SD scan, so a
    row without a successful scan is an explicit missing measurement. Only
    build-included Analyses contribute to projected overflow.
    """
    axis = str(config.build_options.get("variant-reference") or "")
    rows: list[dict[str, str]] = []
    measured: list[tuple[str, int, int, str]] = []
    ancestry_measured: list[tuple[str, int, int]] = []
    missing: list[str] = []
    for outcome in outcomes:
        diagnostics = record_section(outcome.record, "diagnostics")
        ancestry_n = diagnostics.get("ancestry_rows_read", diagnostics.get("rows_read"))
        ancestry_matched = diagnostics.get("ancestry_reference_rows_matched")
        total = diagnostics.get("rows_read")
        axis_matched = diagnostics.get("variant_reference_rows_matched")
        eligible_total = diagnostics.get("build_eligible_rows")
        eligible_on = diagnostics.get("build_eligible_rows_on_variant_reference")
        eligible_off = diagnostics.get("build_eligible_rows_off_variant_reference")
        success = (outcome.record or {}).get("status") == "success"

        def count(value: Any, denominator: Any) -> bool:
            return (type(value) is int and type(denominator) is int
                    and denominator > 0 and 0 <= value <= denominator)

        anc_valid = success and count(ancestry_matched, ancestry_n)
        axis_valid = success and count(axis_matched, total)
        # A record "carries" the post-INFO diagnostics when build_eligible_rows
        # is an int; its on/off split is valid only when it partitions that count.
        eligible_carried = success and type(eligible_total) is int and eligible_total >= 0
        eligible_valid = (
            eligible_carried
            and count(eligible_on, eligible_total)
            and count(eligible_off, eligible_total)
            and eligible_on + eligible_off == eligible_total
        )
        fingerprints = record_section(outcome.record, "fingerprints")
        row = {
            "analysis_id": outcome.analysis_id,
            "ancestry_reference_id": str(fingerprints.get("ancestry_reference_id") or ""),
            "ancestry_rows_scanned": str(ancestry_n) if success and type(ancestry_n) is int and ancestry_n >= 0 else "",
            "ancestry_rows_matched": str(ancestry_matched) if anc_valid else "",
            "ancestry_reference_rate": _float_or_empty(ancestry_matched / ancestry_n) if anc_valid else "",
            "variant_reference": axis,
            "rows_scanned": str(total) if success and type(total) is int and total >= 0 else "",
            "variant_reference_rows_matched": str(axis_matched) if axis and axis_valid else "",
            "variant_reference_rate": _float_or_empty(axis_matched / total) if axis and axis_valid else "",
            "build_eligible_rows": str(eligible_total) if eligible_carried else "",
            "build_eligible_rows_on_variant_reference": str(eligible_on) if axis and eligible_valid else "",
            "build_eligible_rows_off_variant_reference": str(eligible_off) if axis and eligible_valid else "",
            "build_eligible_rate": _float_or_empty(eligible_on / eligible_total) if axis and eligible_valid and eligible_total > 0 else "",
            "stop_reason": str(diagnostics.get("stop_reason") or ""),
            "exclude_from_build": "" if outcome.included else "true",
        }
        rows.append(row)
        if anc_valid:
            ancestry_measured.append((outcome.analysis_id, ancestry_n, ancestry_matched))
        if outcome.included:
            if not anc_valid:
                missing.append(outcome.analysis_id)
            elif axis:
                if eligible_carried:
                    # Prefer the post-INFO denominator; a carried-but-unusable
                    # (for example zero-eligible) count is missing, never a
                    # silent fallback to pre-INFO rows.
                    if eligible_valid and eligible_total > 0:
                        measured.append(
                            (outcome.analysis_id, eligible_total, eligible_on, "build_eligible_rows")
                        )
                    else:
                        missing.append(outcome.analysis_id)
                elif axis_valid:
                    measured.append(
                        (outcome.analysis_id, total, axis_matched, "legacy_rows_read")
                    )
                else:
                    missing.append(outcome.analysis_id)

    errors: list[str] = []
    if missing:
        errors.append("reference overlap missing or invalid for included Analyses: "
                      + ", ".join(missing[:10]))
    bases = sorted({basis for _, _, _, basis in measured})
    basis = bases[0] if len(bases) == 1 else ("mixed" if bases else None)
    summary: dict[str, Any] = {
        "low_overlap_threshold": LOW_OVERLAP_RATE,
        "max_off_reference_share": MAX_OFF_REFERENCE_SHARE,
        "included_measured": len(measured) if axis else sum(
            outcome.included and bool(rows[index]["ancestry_reference_rate"])
            for index, outcome in enumerate(outcomes)
        ),
        "ancestry_median_rate": None,
        "ancestry_p05_rate": None,
        "ancestry_low_overlap_analyses": [],
        "median_rate": None,
        "p05_rate": None,
        "p10_rate": None,
        "low_overlap_count": 0,
        "low_overlap_analyses": [],
        "projected_off_reference_share": None,
        "projected_off_reference_basis": None,
        "worst_contributors": [],
    }
    if ancestry_measured:
        ancestry_rates = sorted(matches / scanned for _, scanned, matches in ancestry_measured)
        summary["ancestry_median_rate"] = statistics.median(ancestry_rates)
        summary["ancestry_p05_rate"] = ancestry_rates[int((len(ancestry_rates) - 1) * .05)]
        summary["ancestry_low_overlap_analyses"] = sorted(
            aid for aid, scanned, matches in ancestry_measured
            if matches / scanned < LOW_OVERLAP_RATE
        )
    if axis and measured:
        rates = sorted(matches / scanned for _, scanned, matches, _ in measured)
        low = sorted(aid for aid, scanned, matched, _ in measured if matched / scanned < LOW_OVERLAP_RATE)
        total_scanned = sum(scanned for _, scanned, _, _ in measured)
        total_matched = sum(matches for _, _, matches, _ in measured)
        off_share = (total_scanned - total_matched) / total_scanned
        contributors = sorted(measured, key=lambda item: (-(item[1] - item[2]), item[0]))
        summary.update(
            median_rate=statistics.median(rates),
            p05_rate=rates[int((len(rates) - 1) * .05)],
            p10_rate=rates[int((len(rates) - 1) * .10)],
            low_overlap_count=len(low), low_overlap_analyses=low,
            projected_off_reference_share=off_share,
            projected_off_reference_basis=basis,
            worst_contributors=[aid for aid, _, _, _ in contributors[:10]],
        )
        if off_share > MAX_OFF_REFERENCE_SHARE:
            errors.append(
                f"projected off-reference share {off_share:.1%} exceeds {MAX_OFF_REFERENCE_SHARE:.0%} "
                f"(denominator basis: {basis}); "
                "worst contributors: " + ", ".join(summary["worst_contributors"])
            )
    if not axis:
        summary["median_rate"] = summary["ancestry_median_rate"]
        summary["p05_rate"] = summary["ancestry_p05_rate"]
        summary["low_overlap_analyses"] = summary["ancestry_low_overlap_analyses"]
        summary["low_overlap_count"] = len(summary["low_overlap_analyses"])
    return rows, summary, errors


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
    trait_ontology = resolved.trait_ontology
    if trait_ontology is None:
        raise CandidateError(
            f"{row.analysis_id}: no Trait Ontology Mapping; run "
            "map_candidate_trait_ontologies after verification (#286)"
        )
    return {
        "analysis_index": str(index),
        "analysis_id": row.analysis_id,
        "source_analysis_id": row.analysis_id,
        "source_label": resolved.source_label or row.trait,
        "analysis_label": resolved.source_label or row.trait,
        "trait_ontology_label": trait_ontology.trait_ontology_label,
        "trait_ontology_id": trait_ontology.trait_ontology_id,
        "trait_ontology_mapping_method": str(trait_ontology.method),
        "source_file": row.data_file,
        "source_reader_capability": config.reader_capability,
        "source_url": row.data_url,
        "source_bundle_id": "",
        "downloaded_file": Path(row.data_file).name if row.data_file else "",
        "checksum": row.sha256,
        "checksum_algorithm": "sha256",
        "size_bytes": row.data_bytes,
        # Collection-wide facts: the Analysis's own value, else the release
        # config's default (#287). Neither is ever guessed here.
        "source_genome_build": _or_default(resolved.source_genome_build, config, "source_genome_build"),
        "license": _or_default(resolved.license, config, "license"),
        "publication_doi": _or_default(resolved.publication_doi, config, "publication_doi"),
        "publication_pmid": resolved.publication_pmid or row.publication_pmid,
        "consortium": _or_default(resolved.consortium, config, "consortium"),
        "first_author": resolved.first_author,
        "source_ancestry_label": resolved.source_ancestry_label,
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
        "info_score_threshold": outcome.info_score_threshold,
        "imputation_score_column": outcome.info_score_column,
        "imputation_score_kind": outcome.info_score_kind,
        "imputation_score_provenance": outcome.info_score_provenance,
        "maf_threshold": outcome.maf_threshold,
        "inclusion_reason": inclusion_reason,
        "exclude_from_build": exclude,
    }


def _or_default(value: str, config: CandidateConfiguration, key: str) -> str:
    """An Analysis's own value, or the release config's collection-wide default."""
    return value or str(config.defaults.get(key, ""))


def _ancestry_row(
    outcome: AnalysisOutcome, config: CandidateConfiguration
) -> dict[str, str]:
    record = outcome.record
    ancestry = record_section(record, "ancestry")
    row: dict[str, str] = {name: "" for name in ANCESTRY_SIDECAR_COLUMNS}
    row["analysis_id"] = outcome.analysis_id
    row["source_analysis_id"] = outcome.analysis_id
    row["source_ancestry_label"] = outcome.metadata.source_ancestry_label
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
    pheno = record_section(record, "phenotype_sd")
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

    if outcome.stored_effect_scale in NON_QUANTITATIVE_EFFECT_SCALES:
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
        "category": controlled_exclusion_categories()[outcome.exclusion_reason],
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


def render_build_yaml(store_id: str, config: CandidateConfiguration) -> dict[str, Any]:
    """Render the observed-only Hybrid Build Recipe as the emitter's ``build.yaml`` document.

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
    # Dense and Hybrid releases declare the maintained hm3 Indexed Variant
    # Subset by default (stores #210). ``None`` is a Ragged release, which emits
    # no key at all; ``[]`` is a Dense/Hybrid release's reviewed opt-out.
    if config.indexed_subsets is not None:
        document["indexed_subsets"] = [dict(entry) for entry in config.indexed_subsets]
    return document


def render_release_yaml(
    *,
    store_id: str,
    config: CandidateConfiguration,
    tables: CandidateTables,
    created_at: str,
    inventory_sha256: str,
    preflight_report: Path,
    index_summary: Mapping[str, Any],
    generator_version: str,
    resolver_receipt_path: Path | None = None,
    resolver_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Render the candidate ``release.yaml`` identity record as the emitter's document.

    Only identity, lineage, status, creation time, source-snapshot identity, the
    executed command log and prose survive (ADR 0029). The evidence the candidate
    binds -- the frozen inventory's checksum, the preflight report, the successful
    resolution receipt, the #152 policy and the executed resolver argv -- is the
    command log plus prose. The emitter records the command log (#248).
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
        f"preflight report {preflight_report}"
        + (
            f"; resolution receipt {resolver_receipt_path} "
            f"(sha256 {resolver_receipt_sha256})"
            if resolver_receipt_path is not None
            else ""
        )
        + "."
    )
    notes_lines.append(
        "Policy: full-reference AF ancestry assignment and source-AF-only quantitative "
        "phenotype-SD estimation (issue #152); case-control rows use log_or/binary_trait; "
        "non-target/unassigned ancestry, orientation failures, unusable source AF and "
        "ordinary resolution failures are controlled exclusions (sidecars/exclusions.tsv)."
    )
    if config.require_maf_filtered:
        notes_lines.append(
            "Store composition (#203): this shared Store admits only Analyses whose "
            "rows were MAF-filtered; an included Analysis with no applied MAF floor "
            "is excluded as not_maf_filtered."
        )
    if tables.exclusion_counts:
        notes_lines.append(
            "Exclusions by reason: "
            + ", ".join(f"{reason}={count}" for reason, count in tables.exclusion_counts.items())
        )
    if config.indexed_subsets_opt_out:
        notes_lines.append(
            "Indexed Variant Subsets (#210): the default hm3 Indexed Variant "
            f"Subset is intentionally omitted: {config.indexed_subsets_opt_out}"
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
            "resolver_receipt": (
                str(resolver_receipt_path) if resolver_receipt_path is not None else None
            ),
            "resolver_receipt_sha256": resolver_receipt_sha256,
            "resolver_opengwasdb_version": index_summary.get("opengwasdb_version"),
        },
        "generator": {"version": generator_version},
        "description": config.description,
        "notes": notes,
    }
    return document


def render_validation_yaml(
    *,
    tables: CandidateTables,
    index_summary: Mapping[str, Any],
    validated_at: str,
    validator_name: str,
    trait_ontology_mapping: Mapping[str, Any],
) -> dict[str, Any]:
    """Render the candidate Validation Record as the emitter's ``validation.yaml`` document.

    No Store was built, so every ``observed`` measurement is ``null``: absence is
    recorded, never guessed (issue #135). The checks describe the Phase B
    evidence that does exist. ``trait_ontology_mapping`` is the Trait Ontology
    Mapping run's provenance (#286): the Canonical Trait Mapping Table's
    identity, the method counts and any rejected source terms.
    """
    status = ("failed" if tables.reference_overlap_errors else
              "passed_with_warnings" if tables.warnings else "passed")
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
            "reference_overlap": "failed" if tables.reference_overlap_errors else "passed",
        },
        "reference_overlap": dict(tables.reference_overlap),
        "store_composition": dict(tables.store_composition),
        "quality_checks": {
            name: summary.as_dict() for name, summary in tables.quality_checks.items()
        },
        "trait_ontology_mapping": dict(trait_ontology_mapping),
        "reports": {
            "source_readiness": "sidecars/source_readiness.tsv",
            "ancestry": "sidecars/ancestry.tsv",
            "sd_estimation": "sidecars/sd_estimation.tsv",
            "exclusions": "sidecars/exclusions.tsv",
            "reference_overlap": "sidecars/reference_overlap.tsv",
            "quality_checks": "sidecars/quality_checks.tsv",
        },
        "warnings": list(tables.warnings),
        "errors": list(tables.reference_overlap_errors),
    }
    return document


# ---------------------------------------------------------------------------
# Generation checks before emitting
# ---------------------------------------------------------------------------


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
    "EFFECT_PLACEHOLDER_EXCLUSION_COLUMNS",
    "MAF_FILTER_EXEMPTION_COLUMNS",
    "REFERENCE_OVERLAP_COLUMNS",
    "USABLE_INFO_SCORE_STATES",
    "USABLE_MAF_STATES",
    "CandidateConfiguration",
    "CandidateError",
    "AnalysisMetadata",
    "CandidateTables",
    "EXCLUSION_COLUMNS",
    "HISTORICAL_RECEIPT_SCHEMA_VERSIONS",
    "POLICY_EXCLUSION_CATEGORIES",
    "RECEIPT_FILENAME",
    "RECEIPT_KEYS",
    "RECEIPT_SCHEMA_VERSION",
    "RESOLVER_MANIFEST_COLUMNS",
    "SD_ESTIMATION_SIDECAR_COLUMNS",
    "SOURCE_READINESS_COLUMNS",
    "SUPERPOPULATIONS",
    "SUPPORTED_RECORD_SCHEMA_VERSION",
    "AnalysisOutcome",
    "ResolverRow",
    "ResolverRun",
    "account_kept_records",
    "account_records",
    "parse_receipt_records",
    "analysis_maf_threshold",
    "apply_release_policy",
    "build_candidate_tables",
    "build_resolution_receipt",
    "compute_resolution_contract",
    "controlled_exclusion_categories",
    "derive_resolver_manifest",
    "info_score_emission",
    "load_candidate_configuration",
    "maf_threshold_emission",
    "now_utc",
    "read_analysis_metadata",
    "trait_sources",
    "map_candidate_trait_ontologies",
    "read_genotyping_technologies",
    "read_effect_placeholder_exclusions",
    "read_maf_filter_exempt_analyses",
    "read_resolution_receipt",
    "render_build_yaml",
    "render_release_yaml",
    "render_resolver_manifest",
    "render_validation_yaml",
    "resolver_argv",
    "resolver_settings",
    "resolver_settings_digest",
    "run_resolver",
    "sha256_file",
    "sha256_text",
    "validate_candidate_analyses",
    "verify_records",
    "write_resolution_receipt",
]
