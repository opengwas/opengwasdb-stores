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
- the run's **runtime** and **Work Root** (``runtime``, ``output``).

Loading is a gate: an unknown key at any level, a missing required field, a
value outside its vocabulary and the legacy Selection grammar are refused,
never ignored or defaulted. ``docs/spec/release-config.md`` is the schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from manifest_generation import quality_checks
from manifest_generation.freeze import frozen_paths
from manifest_generation.indexed_subsets import (
    INDEXED_SUBSETS_KEY,
    INDEXED_SUBSETS_OPT_OUT_KEY,
    IndexedSubsetConfigError,
    resolve_indexed_subsets,
)
from manifest_generation.selection import Selection, parse_selection
from manifest_generation.source_inventory import (
    MethodTier,
    PreflightConfigError,
    ReferenceResource,
)
from ogstores.bundle import SUPERPOPULATIONS

__all__ = [
    "ACCESS_POSTURES",
    "GENOME_BUILDS",
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
#: The spelling every Release Bundle's ``source_genome_build`` uses
#: (``docs/spec/source-connectors.md``).
GENOME_BUILDS: tuple[str, ...] = ("GRCh37", "GRCh38")

#: The kind a Canonical Trait Mapping Table resource declares
#: (``trait_ontology_mapping``'s ``resource.yaml``).
TRAIT_ONTOLOGY_MAPPING_KIND: str = "trait_ontology_mapping"

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
    "quality_checks", "effect_scale_validation", "runtime",
)
DEFAULTS_OPTIONAL = (
    "source_genome_build", "license", "publication_doi", "consortium",
    "sample_size_scope", "analysis_group_id", "info_score_threshold", "maf_threshold",
)
#: The text defaults ``analyses.tsv`` carries where the Source Metadata Table is blank.
TEXT_DEFAULTS = ("source_genome_build", "license", "publication_doi", "consortium",
                 "sample_size_scope", "analysis_group_id")
METHOD_TIER_KEYS = ("stored_effect_scale", "original_effect_scale", "original_sd_method",
                    "sample_size_kind")
RELEASE_POLICY_OPTIONAL = (
    "store_composition", "imputation_score_declarations",
    "maf_filter_exempt_genotyping_technologies", "maf_filter_exempt_analyses",
    "effect_placeholder_exclusions",
)
RESOURCE_REQUIRED = ("resource_id", "kind", "location")
RESOURCE_OPTIONAL = (
    "location_kind", "version", "status", "fine_group_map", "ancestry", "super_populations",
    "genome_build", "variant_id_convention",
)


def _where(path: Path, *keys: str) -> str:
    return f"{path}:" + ".".join(keys) if keys else str(path)


def _block(
    value: Any, where: str, required: Sequence[str] = (), optional: Sequence[str] = ()
) -> Mapping[str, Any]:
    """``value`` as a mapping with every required key and no unknown one."""
    if not isinstance(value, Mapping):
        raise ReleaseConfigError(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in value if key not in required and key not in optional)
    if unknown:
        raise ReleaseConfigError(
            f"{where} has unknown key(s) {', '.join(unknown)}; it reads only "
            + ", ".join(list(required) + list(optional))
        )
    missing = [key for key in required if value.get(key) is None or value.get(key) == ""]
    if missing:
        raise ReleaseConfigError(f"{where} is missing required key(s) {', '.join(missing)}")
    return value


def _text(value: Any, where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ReleaseConfigError(f"{where} must be text, not {value!r}")
    text = str(value)
    if not text.strip():
        raise ReleaseConfigError(f"{where} must not be blank")
    return text


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
    _block(document, str(path), TOP_LEVEL_REQUIRED, TOP_LEVEL_OPTIONAL)

    # -- identity --------------------------------------------------------------
    label = _text(document["label"], _where(path, "label"))
    access_posture = str(document.get("access_posture") or "public")
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
    build = BuildRecipeShape(
        layout=_text(build_block["layout"], f"{where}.layout"),
        completion_state=_text(build_block["completion_state"], f"{where}.completion_state"),
        command=_text(build_block["command"], f"{where}.command"),
        options=dict(build_block.get("options") or {}),
        post=dict(build_block["post"]),
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
    defaults = {
        key: _text(defaults_block[key], f"{where}.{key}")
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
                key: _text(value, f"{where}.by_study_design.{design}.{key}")
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
    if selection_superpopulation not in SUPERPOPULATIONS:
        raise ReleaseConfigError(
            f"{where}.selection_superpopulation {selection_superpopulation!r} is not a "
            f"super-population code ({', '.join(SUPERPOPULATIONS)})"
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
                      ("maf_floor", "extraction_panel", "gates"))
    ancestry_reference_resource_id = declared(
        ancestry["reference_resource_id"], f"{where}.reference_resource_id"
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
        work_root=Path(_text(output["work_root"], _where(path, "output.work_root"))),
        required_resource_ids=tuple(
            dict.fromkeys(
                [ancestry_reference_resource_id]
                + [resource_id for _ancestry, resource_id in effect_scale_references]
                + [trait_mapping_resource_id]
            )
        ),
    )
