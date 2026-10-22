"""The release config: the one connector file written per Store Release (#287, ADR 0036).

A source plugs into Manifest Generation through four files (ADR 0036). Three
are the Source Collection's -- acquisition's Acquisition Manifest, the metadata
adapter's Source Metadata Table and the Column Map -- and the fourth is this
one, written for each Store Release. It says, and nothing else says:

- the release's **identity** (``label``, ``access_posture``, ``description``,
  ``notes``), which ``release.yaml`` carries;
- the **Source Collection** (``source_collection``): its id, the paths of its
  two connector files, and the frozen Source Inventory the release reads;
- the **Selection** (``selection``), in #283's grammar
  (``manifest_generation.selection``);
- the **Build Recipe shape** (``build``, ``indexed_subsets``), which
  ``build.yaml`` carries;
- the **collection-wide defaults** (``defaults``): Analytical Metadata the
  Source Metadata Table leaves blank -- ``source_genome_build``, ``license``,
  ... -- and the method tier for each study design;
- the **release policy** (``release_policy``, ``quality_checks``): the
  Selection's super-population, the Store Composition Rule, the reviewed
  per-Analysis listings and Quality Check threshold overrides (#285);
- the **Reference Resources** (``reference_resources``) and what uses them:
  ancestry assignment, effect-scale validation and Trait Ontology Mapping's
  Canonical Trait Mapping Table;
- for a reference-completed release, the **completion recipe** (``completion``,
  #340): the LD panel it is completed against, the owner decision that
  qualified and activated that panel, and the `complete-dense` flags beside it
  (ADR 0037);
- for a region-filtered release, the **region policy** acquisition filters its
  files to (``region_policy``, #300), with the QC panel it always keeps;
- the run's **runtime** and **Work Root** (``runtime``, ``output``).

Loading is a gate: an unknown key at any level, a missing required field, a
value outside its vocabulary and the legacy Selection grammar are refused,
never ignored or defaulted. ``docs/spec/release-config.md`` is the schema.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import yaml

from manifest_generation import completion, quality_checks
from manifest_generation.config_schema import block as _key_gate, text as _text_gate
from manifest_generation.completion import CompletionError, CompletionRecipe
from manifest_generation.freeze import frozen_paths
from manifest_generation.region_filter import ASSOCIATION_COVERAGES, RegionPolicy
from manifest_generation.indexed_subsets import (
    INDEXED_SUBSETS_KEY,
    INDEXED_SUBSETS_OPT_OUT_KEY,
    IndexedSubsetConfigError,
    resolve_indexed_subsets,
)
from manifest_generation.selection import Selection, parse_selection
from manifest_generation.source_metadata_table import GENOME_BUILDS
from manifest_generation.source_inventory import (
    MethodTier,
    PreflightConfigError,
    ReferenceResource,
)
from ogstores.bundle import (
    INDEXED_SUBSET_OPTIONAL_KEYS,
    INDEXED_SUBSET_REQUIRED_KEYS,
    SUPERPOPULATIONS,
)

__all__ = [
    "ACCESS_POSTURES",
    "BUILD_POST_KEYS",
    "RELEASE_CONFIG_VERSION",
    "BuildRecipeShape",
    "Connectors",
    "ReleaseConfig",
    "ReleaseConfigError",
    "load_release_config",
]

#: The schema this module reads. A config states it, so a later schema can
#: refuse an older config by name rather than misread it.
RELEASE_CONFIG_VERSION: int = 1

ACCESS_POSTURES: tuple[str, ...] = ("public", "controlled", "embargoed")

#: The ``build.post`` steps Store Release Build plans (``ogstores.plan``'s
#: ``_post_steps``), each on or off. The recipe's ``options`` are opaque
#: ``opengwasdb`` flags passed through verbatim (ADR 0023); ``post`` is the
#: registry's own vocabulary, so it is checked. ``ogstores.plan`` is Store
#: Release Build's, which Manifest Generation does not import, so the names
#: are listed here; a test holds them to the planner's.
BUILD_POST_KEYS: tuple[str, ...] = ("top_hits", "overview", "rho", "validate")

#: The kind a Canonical Trait Mapping Table resource declares
#: (``trait_ontology_mapping``'s ``resource.yaml``).
TRAIT_ONTOLOGY_MAPPING_KIND: str = "trait_ontology_mapping"
#: The kind of Reference Resource ancestry assignment measures against.
ANCESTRY_MIXTURE_KIND: str = "ancestry_mixture"
#: The kind of Reference Resource every region policy keeps (#300).
QC_PANEL_KIND: str = "qc_panel"
#: The kind of Reference Resource that lifts a source build to the reference build (#292).
LIFTOVER_CHAIN_KIND: str = "liftover_chain"
#: The reader capability a release names to read its sources through a Column Map.
COLUMN_MAP_CAPABILITY: str = "opengwasdb.column-map"

#: Ancestry-assignment gate defaults: the resolver flags' values when a config
#: names none (``candidate_workflow.resolver_settings``).
DEFAULT_GATES: Mapping[str, Any] = {"tau": 0.50, "delta": 0.20, "n_min": 5000, "residual_max": 0.06}
DEFAULT_ANCESTRY_MAF_FLOOR: float = 0.01
DEFAULT_INFO_SCORE_THRESHOLD: float = 0.6


class ReleaseConfigError(PreflightConfigError):
    """A release config is malformed, incomplete or contradicts itself."""


@dataclass(frozen=True)
class Connectors:
    """The Source Collection's two connector files, which freeze reads."""

    source_metadata_table: Path
    acquisition_manifest: Path


@dataclass(frozen=True)
class BuildRecipeShape:
    """What ``build.yaml`` carries: a registry fact plus ``opengwasdb`` flags (ADR 0023)."""

    layout: str
    completion_state: str
    command: str
    options: Mapping[str, Any]
    post: Mapping[str, Any]


@dataclass(frozen=True)
class ReleaseConfig:
    """One loaded, validated release config."""

    path: Path
    label: str
    access_posture: str
    description: str
    notes: str
    source_collection_id: str
    #: ``None`` when the config names only a frozen inventory: then the
    #: inventory must exist, because there is nothing to freeze it from.
    connectors: Connectors | None
    inventory_snapshot_id: str
    inventory_directory: Path
    selection: Selection
    build: BuildRecipeShape
    #: ``None`` for a Ragged release (no ``indexed_subsets`` key at all).
    indexed_subsets: tuple[Mapping[str, Any], ...] | None
    indexed_subsets_opt_out: str | None
    #: The collection-wide Analytical Metadata defaults, verbatim:
    #: ``source_genome_build``, ``license``, ``publication_doi``,
    #: ``consortium``, ``sample_size_scope``, ``analysis_group_id``.
    defaults: Mapping[str, str]
    method_tiers: Mapping[str, MethodTier]
    info_score_threshold: str
    maf_threshold: str | None
    selection_superpopulation: str
    require_maf_filtered: bool
    #: #287: an Analysis whose Resolution Record shows no allele frequency takes
    #: the Source Metadata Table's super-population as Assigned Ancestry.
    trust_source_ancestry_without_af: bool
    #: #290: an Analysis the ancestry gates left unassigned is kept, unassigned,
    #: as a recorded warning. A measured different super-population still
    #: excludes.
    keep_unassigned_ancestry_as_warning: bool
    imputation_score_declarations: Path | None
    maf_filter_exempt_genotyping_technologies: tuple[str, ...]
    maf_filter_exempt_analyses_path: Path | None
    effect_placeholder_exclusions_path: Path | None
    quality_check_overrides: Mapping[str, Any]
    reference_resources: Mapping[str, ReferenceResource]
    ancestry_reference_resource_id: str
    #: ``ancestry_assignment.maf_floor``, verbatim, as the resolver is passed it.
    ancestry_maf_floor: Any
    ancestry_extraction_panel: str | None
    #: The four gates, verbatim, defaults filled in.
    ancestry_gates: Mapping[str, Any]
    #: ``effect_scale_validation``'s ``maf_min``, ``maf_max`` and
    #: ``dispersion_max``, verbatim; absent keys are absent.
    effect_scale: Mapping[str, Any]
    effect_scale_reference_resources: tuple[tuple[str, str], ...]
    trait_ontology_mapping_resource_id: str
    cores: int
    min_free_gb: float
    work_root: Path
    required_resource_ids: tuple[str, ...] = field(default=())
    #: The region policy acquisition filters this release's files to (#300),
    #: or ``None`` for a release acquired unfiltered.
    region_policy: RegionPolicy | None = None
    #: The ``qc_panel`` Reference Resource the region policy always keeps.
    qc_panel_resource_id: str | None = None
    #: ``column_map.builtin``: the name of a built-in Column Map the release reads through.
    column_map_builtin: str | None = None
    #: ``column_map.path``: the carried Column Map file the release reads through.
    column_map_path: Path | None = None
    #: ``ancestry_assignment.liftover_chain_resource_id``: the declared chain the
    #: resolver lifts a differing source build with.
    liftover_chain_resource_id: str | None = None
    #: The completion recipe a reference-completed release declares (#340,
    #: ADR 0037), or ``None`` for a release that declares no completion block.
    completion: CompletionRecipe | None = None

    # -- what preflight reads ---------------------------------------------------

    #: Every Analysis is resolved (ADR 0036): ancestry assignment and
    #: effect-scale measurement are never switched off.
    ancestry_assignment_enabled = True
    effect_scale_enabled = True

    @property
    def inventory_path(self) -> Path:
        return frozen_paths(self.inventory_directory, self.inventory_snapshot_id).inventory

    @property
    def inventory_provenance_path(self) -> Path:
        return frozen_paths(self.inventory_directory, self.inventory_snapshot_id).provenance

    @property
    def canonical_trait_mapping_table(self) -> Path:
        """The Canonical Trait Mapping Table's resource directory."""
        return Path(self.reference_resources[self.trait_ontology_mapping_resource_id].location)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

TOP_LEVEL_REQUIRED = (
    "release_config_version", "label", "description", "source_collection", "selection",
    "build", "defaults", "release_policy", "reference_resources", "ancestry_assignment",
    "trait_ontology_mapping", "output",
)
TOP_LEVEL_OPTIONAL = (
    "access_posture", "notes", INDEXED_SUBSETS_KEY, INDEXED_SUBSETS_OPT_OUT_KEY,
    "quality_checks", "effect_scale_validation", "runtime", "region_policy", "column_map",
    "completion",
)
#: The Build Recipe shapes the release config reads: observed-only, or one that
#: completes its own Store under its id (ADR 0037).
COMPLETION_STATES: tuple[str, ...] = ("observed_only", "reference_completed")
#: The layout this pipeline emits a single-id completion for (#340). Not a
#: prohibition: ADR 0037's Observed-View Check also covers the Dense Component of
#: Hybrid Stores, and completing a Hybrid one is unported work rather than a
#: refusal the contract makes.
COMPLETION_LAYOUTS: tuple[str, ...] = ("dense",)
DEFAULTS_OPTIONAL = (
    "source_genome_build", "license", "publication_doi", "consortium",
    "sample_size_scope", "analysis_group_id", "info_score_threshold", "maf_threshold",
    "tissue", "context",
)
#: The text defaults ``analyses.tsv`` carries where the Source Metadata Table is blank.
TEXT_DEFAULTS = ("source_genome_build", "license", "publication_doi", "consortium",
                 "sample_size_scope", "analysis_group_id", "tissue", "context")
REGION_POLICY_REQUIRED = (
    "region_policy_id", "association_coverage", "trans_flank_bp", "significant_p", "suggestive_p",
    "suggestive_lead_min_distance_bp", "qc_panel_resource_id",
)
#: Only for ``cis_plus_signals``, and required there.
REGION_POLICY_OPTIONAL = ("cis_flank_bp",)
METHOD_TIER_KEYS = ("stored_effect_scale", "original_effect_scale", "original_sd_method",
                    "sample_size_kind")
RELEASE_POLICY_OPTIONAL = (
    "store_composition", "trust_source_ancestry_without_af", "imputation_score_declarations",
    "maf_filter_exempt_genotyping_technologies", "maf_filter_exempt_analyses",
    "effect_placeholder_exclusions", "keep_unassigned_ancestry_as_warning",
)
RESOURCE_REQUIRED = ("resource_id", "kind", "location")
RESOURCE_OPTIONAL = (
    "location_kind", "version", "status", "fine_group_map", "ancestry", "super_populations",
    "genome_build", "variant_id_convention",
)


def _where(path: Path, *keys: str) -> str:
    return f"{path}:" + ".".join(keys) if keys else str(path)


#: The key gate this schema and the completion recipe share (``config_schema``),
#: each naming the error its own callers catch.
_block = functools.partial(_key_gate, error=ReleaseConfigError)
_text = functools.partial(_text_gate, error=ReleaseConfigError)


def _path(value: Any, where: str, repo_root: Path) -> Path:
    """A path: absolute as written, otherwise relative to the repository root."""
    path = Path(_text(value, where))
    return path if path.is_absolute() else repo_root / path


def _optional_path(block: Mapping[str, Any], key: str, where: str, repo_root: Path) -> Path | None:
    if block.get(key) is None:
        return None
    return _path(block[key], f"{where}.{key}", repo_root)


def _number_in(value: Any, where: str, low: Decimal, high: Decimal) -> str:
    """A number in ``[low, high]``, as its canonical string."""
    if isinstance(value, bool) or value is None or not str(value).strip():
        raise ReleaseConfigError(f"{where} must be a number in [{low},{high}]")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ReleaseConfigError(f"{where} must be a number in [{low},{high}]") from exc
    if not number.is_finite() or not low <= number <= high:
        raise ReleaseConfigError(f"{where} must be a number in [{low},{high}], not {value!r}")
    return format(number, "f")


def _vocabularies() -> dict[str, tuple[str, ...]]:
    """OpenGWASDB's controlled vocabularies a release config's defaults draw on.

    Imported, never copied, so a config cannot drift from what the pinned
    Analysis schema accepts; imported here, not at module load, as
    ``source_metadata_table`` does.
    """
    from opengwasdb.model.enums import (
        OriginalSdMethod,
        SampleSizeKind,
        SampleSizeScope,
        StoredEffectScale,
    )

    return {
        "stored_effect_scale": tuple(member.value for member in StoredEffectScale),
        "original_sd_method": tuple(member.value for member in OriginalSdMethod),
        "sample_size_kind": tuple(member.value for member in SampleSizeKind),
        "sample_size_scope": tuple(member.value for member in SampleSizeScope),
    }


def _in_vocabulary(value: str, where: str, key: str, vocabularies: Mapping[str, tuple[str, ...]]) -> str:
    allowed = vocabularies.get(key)
    if allowed is not None and value not in allowed:
        raise ReleaseConfigError(f"{where} {value!r} is not one of {', '.join(allowed)}")
    return value


def load_release_config(path: Path | str, repo_root: Path | str) -> ReleaseConfig:
    """Load and validate the release config at ``path``; refuse anything the schema does not say."""
    path = Path(path)
    repo_root = Path(repo_root)
    if not path.is_file():
        raise ReleaseConfigError(f"release config not found: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ReleaseConfigError(f"release config {path} is not valid YAML: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ReleaseConfigError(f"release config {path} is not a YAML mapping")

    version = document.get("release_config_version")
    if version != RELEASE_CONFIG_VERSION or isinstance(version, bool):
        raise ReleaseConfigError(
            f"{path}: release_config_version is {version!r}; this Manifest Generation reads "
            f"release_config_version {RELEASE_CONFIG_VERSION} (docs/spec/release-config.md). A "
            "config without it predates the release config schema (#287)"
        )
    if "resolution_root" in (document.get("output") or {}):
        raise ReleaseConfigError(
            f"{path}: output.resolution_root is not a release config key: the resolution root "
            "is deployment configuration (--resolution-root, OPENGWASDB_RESOLUTION_ROOT or "
            "ogstores.yaml), so every release over a Source Collection finds the same "
            "Resolution Area (#284)"
        )
    # Before the key gate below, so the retired keys are refused by name with
    # the decision that retired them, not as an unknown key (#308, #340).
    retired = [key for key in completion.RETIRED_KEYS if key in document]
    if retired:
        raise ReleaseConfigError(
            f"{path} declares {', '.join(retired)}: a reference-completed Store is published "
            "under its own id, and its one bundle declares both phases (ADR 0037, #340). A "
            "reserved completed id is never reused; remove the key."
        )
    _block(document, str(path), TOP_LEVEL_REQUIRED, TOP_LEVEL_OPTIONAL)

    # -- identity --------------------------------------------------------------
    label = _text(document["label"], _where(path, "label"))
    # Defaulted only when the key is absent: an explicit false or blank is a
    # mistake to refuse, not a request for public.
    access_posture = document.get("access_posture", "public")
    if access_posture not in ACCESS_POSTURES:
        raise ReleaseConfigError(
            f"{_where(path, 'access_posture')} {access_posture!r} is not one of "
            + ", ".join(ACCESS_POSTURES)
        )
    description = _text(document["description"], _where(path, "description"))
    notes = str(document.get("notes") or "")

    # -- Source Collection -----------------------------------------------------
    where = _where(path, "source_collection")
    collection = _block(
        document["source_collection"], where, ("source_collection_id", "inventory"), ("connectors",)
    )
    source_collection_id = _text(collection["source_collection_id"], f"{where}.source_collection_id")
    if "/" in source_collection_id or source_collection_id in (".", ".."):
        raise ReleaseConfigError(
            f"{where}.source_collection_id {source_collection_id!r} must be one path segment: it "
            "names the Source Collection's Resolution Area"
        )
    inventory = _block(collection["inventory"], f"{where}.inventory", ("snapshot_id", "directory"))
    connectors = None
    if collection.get("connectors") is not None:
        raw = _block(
            collection["connectors"], f"{where}.connectors",
            ("source_metadata_table", "acquisition_manifest"),
        )
        connectors = Connectors(
            source_metadata_table=_path(raw["source_metadata_table"],
                                        f"{where}.connectors.source_metadata_table", repo_root),
            acquisition_manifest=_path(raw["acquisition_manifest"],
                                       f"{where}.connectors.acquisition_manifest", repo_root),
        )

    # -- Selection (#283's grammar; the legacy grammar is refused by name) -----
    selection = parse_selection(document["selection"], where=_where(path, "selection"))

    # -- Build Recipe shape ----------------------------------------------------
    where = _where(path, "build")
    build_block = _block(document["build"], where, ("layout", "completion_state", "command", "post"),
                         ("options",))
    for key in ("options", "post"):
        if build_block.get(key) is not None and not isinstance(build_block[key], Mapping):
            raise ReleaseConfigError(f"{where}.{key} must be a mapping")
    post_block = _block(build_block["post"], f"{where}.post", (), BUILD_POST_KEYS)
    for key, value in post_block.items():
        if not isinstance(value, bool):
            raise ReleaseConfigError(f"{where}.post.{key} must be true or false, not {value!r}")
    completion_state = _text(build_block["completion_state"], f"{where}.completion_state")
    if completion_state not in COMPLETION_STATES:
        raise ReleaseConfigError(
            f"{where}.completion_state {completion_state!r} is not one of "
            + ", ".join(COMPLETION_STATES)
        )
    build = BuildRecipeShape(
        layout=_text(build_block["layout"], f"{where}.layout"),
        completion_state=completion_state,
        command=_text(build_block["command"], f"{where}.command"),
        options=dict(build_block.get("options") or {}),
        post=dict(build_block["post"]),
    )
    declared_subsets = document.get(INDEXED_SUBSETS_KEY)
    if isinstance(declared_subsets, list):
        for position, entry in enumerate(declared_subsets):
            if isinstance(entry, Mapping):
                _block(
                    entry, _where(path, f"{INDEXED_SUBSETS_KEY}[{position}]"), (),
                    INDEXED_SUBSET_REQUIRED_KEYS + INDEXED_SUBSET_OPTIONAL_KEYS,
                )
    try:
        indexed_subsets, indexed_subsets_opt_out = resolve_indexed_subsets(
            document, build.layout, repo_root
        )
    except IndexedSubsetConfigError as exc:
        raise ReleaseConfigError(f"{path}: {exc}") from exc

    # -- defaults and method tiers ---------------------------------------------
    where = _where(path, "defaults")
    defaults_block = _block(document["defaults"], where, ("by_study_design",), DEFAULTS_OPTIONAL)
    vocabularies = _vocabularies()
    defaults = {
        key: _in_vocabulary(
            _text(defaults_block[key], f"{where}.{key}"), f"{where}.{key}", key, vocabularies
        )
        for key in TEXT_DEFAULTS
        if defaults_block.get(key) is not None
    }
    build_value = defaults.get("source_genome_build")
    if build_value is not None and build_value not in GENOME_BUILDS:
        raise ReleaseConfigError(
            f"{where}.source_genome_build {build_value!r} is not one of {', '.join(GENOME_BUILDS)}"
        )
    tiers_block = defaults_block["by_study_design"]
    if not isinstance(tiers_block, Mapping) or not tiers_block:
        raise ReleaseConfigError(f"{where}.by_study_design must declare at least one method tier")
    method_tiers = {
        str(design): MethodTier(
            study_design=str(design),
            **{
                key: _in_vocabulary(
                    _text(value, f"{where}.by_study_design.{design}.{key}"),
                    f"{where}.by_study_design.{design}.{key}", key, vocabularies,
                )
                for key, value in _block(
                    tier, f"{where}.by_study_design.{design}", METHOD_TIER_KEYS
                ).items()
            },
        )
        for design, tier in tiers_block.items()
    }
    info_score_threshold = _number_in(
        defaults_block.get("info_score_threshold", DEFAULT_INFO_SCORE_THRESHOLD),
        f"{where}.info_score_threshold", Decimal(0), Decimal(1),
    )
    maf_threshold = (
        None
        if defaults_block.get("maf_threshold") is None
        else _number_in(defaults_block["maf_threshold"], f"{where}.maf_threshold",
                        Decimal(0), Decimal("0.5"))
    )

    # -- release policy ----------------------------------------------------------
    where = _where(path, "release_policy")
    policy = _block(document["release_policy"], where, ("selection_superpopulation",),
                    RELEASE_POLICY_OPTIONAL)
    selection_superpopulation = _text(
        policy["selection_superpopulation"], f"{where}.selection_superpopulation"
    )
    if selection_superpopulation not in SUPERPOPULATIONS and (
        selection_superpopulation != quality_checks.MULTI_ANCESTRY_SELECTION
    ):
        raise ReleaseConfigError(
            f"{where}.selection_superpopulation {selection_superpopulation!r} is not a "
            f"super-population code ({', '.join(SUPERPOPULATIONS)}); a Selection that names "
            f"no single super-population says "
            f"{quality_checks.MULTI_ANCESTRY_SELECTION!r} (#290)"
        )
    require_maf_filtered = False
    if policy.get("store_composition") is not None:
        composition = _block(policy["store_composition"], f"{where}.store_composition", (),
                             ("require_maf_filtered",))
        require_maf_filtered = composition.get("require_maf_filtered", False)
        if not isinstance(require_maf_filtered, bool):
            raise ReleaseConfigError(
                f"{where}.store_composition.require_maf_filtered must be a boolean (true or false)"
            )
        if require_maf_filtered and (maf_threshold is None or Decimal(maf_threshold) == 0):
            raise ReleaseConfigError(
                f"{where}.store_composition.require_maf_filtered needs a positive "
                "defaults.maf_threshold; with no floor (or a disabled floor of 0) no Analysis "
                "can be MAF-filtered (#203)"
            )
    trust_source = policy.get("trust_source_ancestry_without_af", False)
    if not isinstance(trust_source, bool):
        raise ReleaseConfigError(
            f"{where}.trust_source_ancestry_without_af must be a boolean (true or false)"
        )
    if trust_source and maf_threshold is None:
        raise ReleaseConfigError(
            f"{where}.trust_source_ancestry_without_af needs a defaults.maf_threshold (0 "
            "filters nothing): only under a MAF request does the Resolution Record count the "
            "rows with no allele frequency, the evidence that a source has none (#287)"
        )
    keep_unassigned = policy.get("keep_unassigned_ancestry_as_warning", False)
    if not isinstance(keep_unassigned, bool):
        raise ReleaseConfigError(
            f"{where}.keep_unassigned_ancestry_as_warning must be a boolean (true or false)"
        )
    if keep_unassigned and selection_superpopulation == quality_checks.MULTI_ANCESTRY_SELECTION:
        raise ReleaseConfigError(
            f"{where}.keep_unassigned_ancestry_as_warning has nothing to keep in a "
            f"{quality_checks.MULTI_ANCESTRY_SELECTION!r} Selection, which never assigns "
            "ancestry (#290)"
        )
    technologies = policy.get("maf_filter_exempt_genotyping_technologies")
    if technologies is None:
        technologies = ()
    elif not isinstance(technologies, list) or any(
        not isinstance(item, str) or not item.strip() for item in technologies
    ):
        raise ReleaseConfigError(
            f"{where}.maf_filter_exempt_genotyping_technologies must be a list of non-empty "
            "technology labels"
        )
    try:
        overrides = quality_checks.parse_overrides(
            document.get("quality_checks"), _where(path, "quality_checks")
        )
    except quality_checks.QualityCheckError as exc:
        raise ReleaseConfigError(str(exc)) from exc

    # -- Reference Resources -------------------------------------------------------
    where = _where(path, "reference_resources")
    raw_resources = document["reference_resources"]
    if not isinstance(raw_resources, list) or not raw_resources:
        raise ReleaseConfigError(f"{where} must be a non-empty list of Reference Resources")
    resources: dict[str, ReferenceResource] = {}
    for position, entry in enumerate(raw_resources):
        entry = _block(entry, f"{where}[{position}]", RESOURCE_REQUIRED, RESOURCE_OPTIONAL)
        resource_id = _text(entry["resource_id"], f"{where}[{position}].resource_id")
        if resource_id in resources:
            raise ReleaseConfigError(f"{where} declares resource_id {resource_id!r} twice")
        fine_group_map = entry.get("fine_group_map")
        resources[resource_id] = ReferenceResource(
            resource_id=resource_id,
            kind=_text(entry["kind"], f"{where}[{position}].kind"),
            status=str(entry.get("status", "available")),
            location=str(_path(entry["location"], f"{where}[{position}].location", repo_root)),
            location_kind=str(entry.get("location_kind", "")),
            version=str(entry.get("version", "")),
            auxiliary_paths=(
                (("fine_group_map", str(_path(fine_group_map, f"{where}[{position}].fine_group_map",
                                              repo_root))),)
                if fine_group_map
                else ()
            ),
        )

    def declared(resource_id: Any, where: str, kind: str | None = None) -> str:
        resource_id = _text(resource_id, where)
        if resource_id not in resources:
            raise ReleaseConfigError(
                f"{where} {resource_id!r} is not a declared Reference Resource "
                f"(reference_resources: {', '.join(resources)})"
            )
        if kind is not None and resources[resource_id].kind != kind:
            raise ReleaseConfigError(
                f"{where} {resource_id!r} is a {resources[resource_id].kind!r} resource, "
                f"not a {kind!r} one"
            )
        return resource_id

    where = _where(path, "ancestry_assignment")
    ancestry = _block(document["ancestry_assignment"], where, ("reference_resource_id",),
                      ("maf_floor", "extraction_panel", "gates", "liftover_chain_resource_id"))
    ancestry_reference_resource_id = declared(
        ancestry["reference_resource_id"], f"{where}.reference_resource_id", ANCESTRY_MIXTURE_KIND
    )
    liftover_chain_resource_id = (
        declared(ancestry["liftover_chain_resource_id"],
                 f"{where}.liftover_chain_resource_id", LIFTOVER_CHAIN_KIND)
        if ancestry.get("liftover_chain_resource_id") is not None
        else None
    )
    gates = dict(DEFAULT_GATES)
    if ancestry.get("gates") is not None:
        gates.update(_block(ancestry["gates"], f"{where}.gates", (), tuple(DEFAULT_GATES)))
    extraction_panel = ancestry.get("extraction_panel")

    where = _where(path, "effect_scale_validation")
    effect_scale_block = _block(
        document.get("effect_scale_validation") or {}, where, (),
        ("maf_min", "maf_max", "dispersion_max", "reference_resources"),
    )
    effect_scale_references = []
    for position, entry in enumerate(effect_scale_block.get("reference_resources") or []):
        entry = _block(entry, f"{where}.reference_resources[{position}]", ("ancestry", "resource_id"))
        effect_scale_references.append(
            (
                _text(entry["ancestry"], f"{where}.reference_resources[{position}].ancestry"),
                declared(entry["resource_id"], f"{where}.reference_resources[{position}].resource_id"),
            )
        )

    where = _where(path, "trait_ontology_mapping")
    trait_mapping = _block(document["trait_ontology_mapping"], where, ("reference_resource_id",))
    trait_mapping_resource_id = declared(
        trait_mapping["reference_resource_id"], f"{where}.reference_resource_id",
        TRAIT_ONTOLOGY_MAPPING_KIND,
    )

    region_policy = qc_panel_resource_id = None
    if document.get("region_policy") is not None:
        region_policy, qc_panel_resource_id = _region_policy(
            document["region_policy"], _where(path, "region_policy"), declared
        )

    # -- Column Map (#292) -----------------------------------------------------------
    column_map_builtin = column_map_path = None
    if document.get("column_map") is not None:
        where = _where(path, "column_map")
        block = _block(document["column_map"], where, (), ("builtin", "path"))
        if (block.get("builtin") is None) == (block.get("path") is None):
            raise ReleaseConfigError(
                f"{where} names exactly one of builtin (a built-in map's name) or path "
                "(a carried map file)"
            )
        if block.get("builtin") is not None:
            column_map_builtin = _text(block["builtin"], f"{where}.builtin")
        else:
            column_map_path = _path(block["path"], f"{where}.path", repo_root)
    reader = build.options.get("source-reader-capability")
    if (column_map_builtin or column_map_path) and reader != COLUMN_MAP_CAPABILITY:
        raise ReleaseConfigError(
            f"{_where(path, 'column_map')} is read only through {COLUMN_MAP_CAPABILITY}, but "
            f"build.options.source-reader-capability is {reader!r}"
        )
    if reader == COLUMN_MAP_CAPABILITY and not (column_map_builtin or column_map_path):
        raise ReleaseConfigError(
            f"build.options.source-reader-capability {COLUMN_MAP_CAPABILITY} needs a "
            f"{_where(path, 'column_map')} naming the map"
        )

    # -- reference completion (ADR 0037, #340) ---------------------------------
    recipe = None
    if "completion" in document:
        where = _where(path, "completion")
        if build.layout not in COMPLETION_LAYOUTS:
            raise ReleaseConfigError(
                f"{where} is declared for a {build.layout!r} layout: Dense completion is the only "
                "completion path Manifest Generation ports so far (ADR 0037's Observed-View Check "
                "also covers the Dense Component of Hybrid Stores), so a release config may "
                "declare it on the " + ", ".join(COMPLETION_LAYOUTS) + " layout only"
            )
        try:
            recipe = completion.parse_completion(
                document["completion"],
                where=where,
                superpopulation=selection_superpopulation,
                resources=resources,
                repo_root=repo_root,
            )
        except CompletionError as exc:
            raise ReleaseConfigError(str(exc)) from exc
    # The state and the recipe are one statement: a config either completes its
    # Store and says against what, or is Observed-Only by policy and declares no
    # recipe. Reading a contradiction as "never completes" would emit an
    # Observed-Only bundle for a Store its author meant to complete (#340).
    if build.completion_state == "reference_completed" and recipe is None:
        raise ReleaseConfigError(
            f"{_where(path, 'build', 'completion_state')} is 'reference_completed', but the "
            "config declares no completion block: a reference-completed Store says which "
            "reference it completes against (ADR 0037, docs/spec/release-config.md)"
        )
    if build.completion_state == "observed_only" and recipe is not None:
        raise ReleaseConfigError(
            f"{_where(path, 'build', 'completion_state')} is 'observed_only' and the config "
            "also declares a completion block: those contradict each other. Declare "
            "'reference_completed' to complete, or no completion block at all (ADR 0037)"
        )

    # -- runtime and Work Root -----------------------------------------------------
    runtime = _block(document.get("runtime") or {}, _where(path, "runtime"), (),
                     ("cores", "min_free_gb"))
    cores = runtime.get("cores", 1)
    if type(cores) is not int or cores < 1:
        raise ReleaseConfigError(f"{_where(path, 'runtime.cores')} must be a whole number >= 1")
    min_free_gb = runtime.get("min_free_gb", 0)
    if isinstance(min_free_gb, bool) or not isinstance(min_free_gb, (int, float)) or min_free_gb < 0:
        raise ReleaseConfigError(f"{_where(path, 'runtime.min_free_gb')} must be a number >= 0")
    output = _block(document["output"], _where(path, "output"), ("work_root",))

    return ReleaseConfig(
        path=path,
        label=label,
        access_posture=access_posture,
        description=description,
        notes=notes,
        source_collection_id=source_collection_id,
        connectors=connectors,
        inventory_snapshot_id=_text(inventory["snapshot_id"], "source_collection.inventory.snapshot_id"),
        inventory_directory=_path(inventory["directory"], "source_collection.inventory.directory",
                                  repo_root),
        selection=selection,
        build=build,
        indexed_subsets=tuple(indexed_subsets) if indexed_subsets is not None else None,
        indexed_subsets_opt_out=indexed_subsets_opt_out,
        defaults=defaults,
        method_tiers=method_tiers,
        info_score_threshold=info_score_threshold,
        maf_threshold=maf_threshold,
        selection_superpopulation=selection_superpopulation,
        require_maf_filtered=require_maf_filtered,
        trust_source_ancestry_without_af=trust_source,
        keep_unassigned_ancestry_as_warning=keep_unassigned,
        imputation_score_declarations=_optional_path(
            policy, "imputation_score_declarations", _where(path, "release_policy"), repo_root
        ),
        maf_filter_exempt_genotyping_technologies=tuple(item.strip() for item in technologies),
        maf_filter_exempt_analyses_path=_optional_path(
            policy, "maf_filter_exempt_analyses", _where(path, "release_policy"), repo_root
        ),
        effect_placeholder_exclusions_path=_optional_path(
            policy, "effect_placeholder_exclusions", _where(path, "release_policy"), repo_root
        ),
        quality_check_overrides=overrides,
        reference_resources=resources,
        ancestry_reference_resource_id=ancestry_reference_resource_id,
        ancestry_maf_floor=ancestry.get("maf_floor", DEFAULT_ANCESTRY_MAF_FLOOR),
        ancestry_extraction_panel=str(extraction_panel) if extraction_panel else None,
        ancestry_gates=gates,
        effect_scale={
            key: effect_scale_block[key]
            for key in ("maf_min", "maf_max", "dispersion_max")
            if key in effect_scale_block
        },
        effect_scale_reference_resources=tuple(effect_scale_references),
        trait_ontology_mapping_resource_id=trait_mapping_resource_id,
        cores=cores,
        min_free_gb=float(min_free_gb),
        work_root=_path(output["work_root"], _where(path, "output.work_root"), repo_root),
        required_resource_ids=tuple(
            dict.fromkeys(
                [ancestry_reference_resource_id]
                + [resource_id for _ancestry, resource_id in effect_scale_references]
                + [trait_mapping_resource_id]
                + ([qc_panel_resource_id] if qc_panel_resource_id else [])
                + ([liftover_chain_resource_id] if liftover_chain_resource_id else [])
                # The panel a release completes against, and only when its
                # reference is qualified and active: a blocked reference is never
                # opened, so its panel is not required here (#340).
                + ([recipe.panel.resource_id] if recipe and recipe.panel else [])
            )
        ),
        region_policy=region_policy,
        qc_panel_resource_id=qc_panel_resource_id,
        column_map_builtin=column_map_builtin,
        column_map_path=column_map_path,
        liftover_chain_resource_id=liftover_chain_resource_id,
        completion=recipe,
    )


def _region_policy(value: Any, where: str, declared) -> tuple[RegionPolicy, str]:
    """``region_policy`` (#300): what acquisition filters this release's files to.

    The policy is the R ``gwas-ssf-ragged`` generator's (``manifest_generation.region_filter``).
    A cis flank belongs to ``cis_plus_signals`` and only to it, and every
    policy keeps a declared ``qc_panel`` resource.
    """
    block = _block(value, where, REGION_POLICY_REQUIRED, REGION_POLICY_OPTIONAL)
    coverage = _text(block["association_coverage"], f"{where}.association_coverage")
    if coverage not in ASSOCIATION_COVERAGES:
        raise ReleaseConfigError(
            f"{where}.association_coverage {coverage!r} is not one of {', '.join(ASSOCIATION_COVERAGES)}"
        )

    def bases(key: str) -> int:
        number = block[key]
        if type(number) is not int or number < 1:
            raise ReleaseConfigError(f"{where}.{key} must be a whole number of bases >= 1, not {number!r}")
        return number

    def probability(key: str) -> float:
        number = block[key]
        try:
            parsed = float(str(number)) if not isinstance(number, bool) else math.nan
        except ValueError:
            parsed = math.nan
        if not 0 < parsed <= 1:
            raise ReleaseConfigError(f"{where}.{key} must be a p-value in (0, 1], not {number!r}")
        return parsed

    significant_p, suggestive_p = probability("significant_p"), probability("suggestive_p")
    if significant_p > suggestive_p:
        raise ReleaseConfigError(
            f"{where}.significant_p {significant_p} is above suggestive_p {suggestive_p}: a "
            "significant hit must also be suggestive"
        )
    cis_flank_bp = None
    if coverage == "cis_plus_signals":
        if block.get("cis_flank_bp") is None:
            raise ReleaseConfigError(f"{where}.cis_flank_bp is required for cis_plus_signals")
        cis_flank_bp = bases("cis_flank_bp")
    elif block.get("cis_flank_bp") is not None:
        raise ReleaseConfigError(
            f"{where}.cis_flank_bp is only for cis_plus_signals; this policy is {coverage}"
        )
    policy = RegionPolicy(
        region_policy_id=_text(block["region_policy_id"], f"{where}.region_policy_id"),
        association_coverage=coverage,
        trans_flank_bp=bases("trans_flank_bp"),
        significant_p=significant_p,
        suggestive_p=suggestive_p,
        suggestive_lead_min_distance_bp=bases("suggestive_lead_min_distance_bp"),
        cis_flank_bp=cis_flank_bp,
    )
    return policy, declared(block["qc_panel_resource_id"], f"{where}.qc_panel_resource_id", QC_PANEL_KIND)
