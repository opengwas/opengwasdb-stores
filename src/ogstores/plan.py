"""The seam: a Release Bundle in, a sequence of `opengwasdb` argv out.

    plan(bundle) -> [Step(name, argv, inputs, outputs)]

A pure function. It reads `build.yaml` and `release.yaml`; it opens no Store,
reads no `analyses.tsv` row, and performs no I/O beyond path construction, so
it is deterministic and testable by string comparison.

Dispatch is a table keyed on the `opengwasdb` subcommand, because the
subcommand is what actually determines the argv shape. `(layout,
completion_state)` is validated as a supported pair and selects the default
subcommand, but it does not select code: a bundle naming `build-ragged-besd`
gets the BESD argv shape whatever else it declares.

`build.options` keys are `opengwasdb` flag names rendered verbatim -- this
module never interprets one (ADR 0023). The only arguments it composes are the
registry's own facts: the Store Release's single `OGS-` identity, the derived
build manifest path, and artifact paths. The same `OGS-` id is passed as both
the `opengwasdb` Store identity and Release identity; Store Family is not read
by the planner (issue #129). The artifact root those paths hang from is
deployment configuration, resolved by `paths.artifact_root()` and passed in;
`plan()` never reads it from a Build Recipe (issue #126).

The build manifest is derived, not authored: `manifest.py` writes it and
the workflow builds it first, so an `exclude_from_build` audit row never reaches
`opengwasdb` (ADR 0025).

`post` keys that are identical across every Release are defaults rather than
restated values; see `POST_DEFAULTS` (issue #128).

A Build Recipe may declare an optional `variant_reference` pre-build block for
the Dense-VCF and Hybrid builders. When declared, `plan()` prepends an
`extract-variant-reference` Step ahead of the build and appends the declared
artifact to the build step's inputs. The declared destination must equal the
build option's `--variant-reference` path, and existence is a runtime fact: the
planner stays pure and never touches the filesystem (#145/#147).

A Build Recipe may also declare optional named `indexed_subsets` (stores #209).
Each declaration carries a subset `name`, the maintained `resource_id`, the
resolved `variant_list` path and `reference_assembly` it uses. `plan()` emits one
`build-indexed-subset` Step per declaration, after the Store and its inline
index steps exist and before `overview` and `validate`, so overview reports the
subset and validate audits it. `plan()` never opens the resource or the variant
list; the release entry point verifies the maintained resource at run time
(`ogstores.resources`). Ragged releases reject any declaration rather than
silently dropping it.

A `reference_completed` release with no `derived_from` completes its own
Store under its id (ADR 0037). `plan()` returns two phases: the build phase,
exactly what the same bundle plans as `observed_only`, then the completion
phase, `complete` from the id's published Store into its staged Store
followed by the declared post steps under `completion-` record names, and
last the Observed-View Check, which gates the replacement of the published
Store. A Ragged release's check also takes the LD panel and ancestry its
`complete` block declares. A two-id child (`derived_from`) plans as before.

This is the only place in the repository that knows how to invoke
`opengwasdb`, which is why the Snakefile's rules and the master list's
`build_command` are two renderings of one thing and cannot disagree.

See docs/spec/store-release-workflow.md and ADRs 0022, 0023, 0024.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ogstores import paths
from ogstores.bundle import (
    INDEXED_SUBSETS_KEY,
    INDEXED_SUBSET_LAYOUTS,
    INDEXED_SUBSET_NAME_RE,
    INDEXED_SUBSET_REQUIRED_KEYS,
    INDEXED_SUBSET_RESERVED_OPTION_KEYS,
    Bundle,
    indexed_subset_step_name,
    is_valid_indexed_subset_name,
    normalize_option_key,
    reserved_options,
)


BUILD_PHASE: str = "build"
COMPLETION_PHASE: str = "completion"


@dataclass(frozen=True)
class Step:
    """One executable opengwasdb step with explicit argv, inputs, and outputs.

    `phase` is `build` for every step a release planned before ADR 0037,
    a two-id child's `complete` included, and `completion` for the second
    phase of a release that completes its own Store. `published_store_arg`
    is the argv index of the token naming that release's published Store,
    which a completion-phase step reads while it writes the staged one: the
    two tokens are the same canonical path, so staging is told which one to
    leave alone (`run.rewrite_argv_for_staging`). It is None for every step
    that names only the Store it writes.
    """

    name: str
    argv: list[str]
    inputs: list[Path]
    outputs: list[Path]
    phase: str = BUILD_PHASE
    published_store_arg: int | None = None


@dataclass(frozen=True)
class IndexedSubsetDeclaration:
    """One declared Indexed Variant Subset, resolved from a Build Recipe.

    Pure data: `plan()` builds the Step from it and never opens `variant_list`
    or the `resource_id` declaration. The checksum is optional provenance the
    runtime verifier cross-checks against the maintained resource.
    """

    name: str
    resource_id: str
    variant_list: Path
    reference_assembly: str
    checksum: str | None
    options: dict[str, Any]


@dataclass(frozen=True)
class CommandSpec:
    """How one `opengwasdb` subcommand is invoked, and what may follow it.

    `name` is the label used in post-step policy errors. `positionals` and
    `inputs` are drawn from a fixed token vocabulary resolved by `_resolve`.
    `check_options` names the completion options the Observed-View Check is
    given too, rendered as `complete` renders them, when the release completes
    its own Store; a `complete` block must declare each.
    """

    phase: str
    name: str
    positionals: tuple[str, ...]
    inputs: tuple[str, ...]
    identity: bool = True
    analyses_flag: bool = False
    top_hits_cmd: str | None = None
    rho_cmd: str | None = None
    overview_cmd: str | None = None
    check_options: tuple[str, ...] = ()


COMMANDS: dict[str, CommandSpec] = {
    "build-dense-vcf": CommandSpec(
        phase="build",
        name="dense",
        positionals=("analyses", "store"),
        inputs=("analyses",),
        top_hits_cmd="build-dense-top-hits",
        rho_cmd="build-dense-rho",
        overview_cmd="regenerate-overview",
    ),
    # Hybrid builds top-hit indexes inline during build-hybrid; rho is dense-only.
    "build-hybrid": CommandSpec(
        phase="build",
        name="hybrid",
        positionals=("analyses", "store"),
        inputs=("analyses",),
        overview_cmd="regenerate-overview",
    ),
    "build-ragged-ssf": CommandSpec(
        phase="build",
        name="ragged-ssf",
        positionals=("analyses", "source", "store"),
        inputs=("analyses",),
        top_hits_cmd="build-ragged-top-hits",
    ),
    # BESD builds top-hit indexes inline, and takes analyses as a flag not a positional.
    "build-ragged-besd": CommandSpec(
        phase="build",
        name="ragged-besd",
        positionals=("besd_prefix", "store"),
        inputs=("besd_files", "analyses"),
        analyses_flag=True,
    ),
    # Every completion command builds top-hit indexes inline.
    "complete-dense": CommandSpec(
        phase="complete",
        name="dense-completed",
        positionals=("parent_store", "store"),
        inputs=("parent_store",),
        identity=False,
        rho_cmd="build-dense-rho",
        overview_cmd="regenerate-overview",
    ),
    "complete-hybrid": CommandSpec(
        phase="complete",
        name="hybrid-completed",
        positionals=("parent_store", "store"),
        inputs=("parent_store",),
        identity=False,
        overview_cmd="regenerate-overview",
    ),
    # The Ragged Observed-View Check rebuilds the regions completion retained
    # from the same LD panel, and is refused without it. Neither command needs
    # the ancestry (`complete-ragged` defaults it to EUR), but a completing
    # Ragged bundle must declare it, so that a silent default cannot complete
    # a non-EUR Store against the wrong panel; the check then cross-checks it
    # against the completed Store's record.
    "complete-ragged": CommandSpec(
        phase="complete",
        name="ragged-completed",
        positionals=("parent_store", "store"),
        inputs=("parent_store",),
        identity=False,
        check_options=("ld-panel", "ancestry"),
    ),
}

# Build-phase layouts that accept `--variant-reference`, and the single build
# command each may run, and therefore may be preceded by an
# `extract-variant-reference` pre-build step (#145/#147/#148).
VARIANT_REFERENCE_LAYOUT_COMMANDS: dict[str, str] = {
    "dense": "build-dense-vcf",
    "hybrid": "build-hybrid",
}
VARIANT_REFERENCE_COMMANDS: frozenset[str] = frozenset(VARIANT_REFERENCE_LAYOUT_COMMANDS.values())

# The optional Build Recipe block that declares the pre-stage, and the keys it
# may use to name the destination path. A bare string is also accepted as
# shorthand for `{output: <string>}`.
VARIANT_REFERENCE_BLOCK_KEYS: tuple[str, ...] = ("variant_reference", "variant-reference")
VARIANT_REFERENCE_OUTPUT_KEYS: tuple[str, ...] = ("output", "path", "output_path", "output-path")

# Optional named Indexed Variant Subset declarations (#209). Each declaration
# becomes one `build-indexed-subset` Step after the Store exists and before
# overview and validation, so overview reports the subset and validate audits
# it. `plan()` resolves no resource: the declaration records the concrete
# `variant_list` path and `reference_assembly` it uses (bundle.INDEXED_SUBSET_*),
# and the release entry point verifies the maintained resource at run time
# (`ogstores.resources`) so the planner stays a pure function.
INDEXED_SUBSET_COMMAND: str = "build-indexed-subset"
INDEXED_SUBSET_OPTIONS_KEY: str = "options"
INDEXED_SUBSET_CHECKSUM_KEY: str = "checksum"
_SHA256_RE: re.Pattern[str] = re.compile(r"\A[0-9a-fA-F]{64}\Z")

DEFAULT_COMMANDS: dict[tuple[str, str], str] = {
    ("dense", "observed_only"): "build-dense-vcf",
    ("hybrid", "observed_only"): "build-hybrid",
    ("ragged", "observed_only"): "build-ragged-ssf",
    ("dense", "reference_completed"): "complete-dense",
    ("hybrid", "reference_completed"): "complete-hybrid",
    ("ragged", "reference_completed"): "complete-ragged",
}

# Post-processing keys that are identical across every committed Release Bundle,
# so a Build Recipe states only the ones it chooses differently from these
# (issue #128). An explicit value in the recipe still wins.
#
# `top_hits` and `overview` are deliberately absent: both vary per Release, and
# `overview` is a real per-layout choice -- Ragged's closed Store envelope
# excludes `overview.html`, so defaulting it on would silently re-enable a step
# the planner then refuses (commit 6092fee, "Reject overviews for Ragged store
# releases").
POST_DEFAULTS: dict[str, Any] = {
    "rho": False,
    "validate": True,
}

# A release that completes its own Store (ADR 0037) is published twice under
# one id, so each phase ends in its own `register`. The completion phase's
# records are named apart from the build phase's, and both sets stay in
# `records/` after the swap: a later run then finds every record and does
# nothing, and a failed completion leaves the build phase's records as they
# were. Its first step keeps the name `complete`, so `register` still
# accepts a `complete-dense-resume` substitution for it.
COMPLETION_STEP_PREFIX: str = "completion-"
REGISTER_STEPS: dict[str, str] = {
    BUILD_PHASE: "register",
    COMPLETION_PHASE: f"{COMPLETION_STEP_PREFIX}register",
}
# The completed Store keeps the Store identity it inherits from its source,
# the id, and takes a release identity of its own (opengwasdb ADR 0011),
# following the Hybrid Dense Component's `<store-id>-dense`.
COMPLETED_RELEASE_SUFFIX: str = "-completed"
# Only the layouts the Observed-View Check can compare complete their own
# Store; any other would keep needing a two-id child (ADR 0037).
SINGLE_ID_COMPLETION_LAYOUTS: frozenset[str] = frozenset({"dense", "hybrid", "ragged"})
# The Observed-View Check compares the published Store with the staged
# completed one before the completion phase publishes (ADR 0037, Decision 2).
# Its seed is one fixed default for every release, the ADR's benchmark seed,
# so each verdict is reproducible from its record; it is not a `build.yaml`
# option. The verdict is written under the release's work directory, not in
# `records/`, and the step's record holds it as the command's stdout.
OBSERVED_VIEW_CHECK_COMMAND: str = "check-observed-view"
OBSERVED_VIEW_CHECK_STEP: str = "completion-observed-view-check"
OBSERVED_VIEW_CHECK_SEED: int = 20261021
OBSERVED_VIEW_CHECK_N_RANDOM: int = 1000
OBSERVED_VIEW_VERDICT_FILENAME: str = "observed-view-check.json"


def completion_step_name(name: str) -> str:
    """The record name a build-phase step's counterpart takes in the completion phase."""
    return f"{COMPLETION_STEP_PREFIX}{name}"


def release_register(bundle: Bundle) -> str:
    """The register record that finishes a release: its last phase's (ADR 0037)."""
    return REGISTER_STEPS[COMPLETION_PHASE if bundle.completes_own_store else BUILD_PHASE]


def render_options(options: dict[str, Any] | None) -> list[str]:
    """Render an options dict into CLI argv tokens without interpretation (ADR 0023).

    Preserves option keys exactly as declared apart from adding the leading '--'.
    Booleans are rendered as bare flags: True -> `--<key>`, False -> `--no-<key>`.
    List-valued options repeat the flag for each item.
    """
    if not options:
        return []
    argv: list[str] = []
    for key, value in options.items():
        if value is None:
            continue
        raw_key = normalize_option_key(key)
        flag = f"--{raw_key}"
        if isinstance(value, bool):
            argv.append(flag if value else f"--no-{raw_key}")
        elif isinstance(value, (list, tuple)):
            for item in value:
                argv.extend([flag, str(item)])
        else:
            argv.extend([flag, str(value)])
    return argv


def _resolve_artifact_root(artifact_root: Path | str | None = None) -> Path:
    """Determine the artifact root for path construction.

    The Build Recipe no longer carries an artifact root: it is deployment
    configuration (issue #126). Callers pass the root resolved by
    `paths.artifact_root()`; without one, fall back to the built-in default so
    `plan()` remains a pure function of its inputs.
    """
    if artifact_root is not None:
        return Path(artifact_root)
    return paths.DEFAULT_ARTIFACT_ROOT


def _besd_prefix(bundle: Bundle) -> Path:
    """BESD source prefix recorded in `release.yaml:source_snapshot.besd_prefix`.

    A BESD triple is a fixed build input that lives outside the artifact root,
    so its location is a bundle fact frozen when the bundle was generated,
    not a path derived from the artifact layout.
    """
    source_snapshot = bundle.release.get("source_snapshot")
    if not isinstance(source_snapshot, dict):
        raise ValueError(
            f"Bundle {bundle.store_id} requires a 'source_snapshot' mapping in "
            f"release.yaml carrying a non-empty 'besd_prefix'"
        )
    prefix = source_snapshot.get("besd_prefix")
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError(
            f"Bundle {bundle.store_id} requires a non-empty string "
            f"'source_snapshot.besd_prefix' in release.yaml"
        )
    return Path(prefix)


def _resolve(token: str, bundle: Bundle, root: Path) -> list[Path]:
    """Resolve one positional/input token to concrete paths."""
    if token == "analyses":
        # The derived build manifest, not the bundle's audit `analyses.tsv`:
        # `exclude_from_build` rows must never reach the builder (ADR 0025).
        return [paths.build_manifest_path(bundle.store_id, root=root)]
    if token == "store":
        return [paths.store_path(bundle.store_id, root=root)]
    if token == "source":
        return [paths.source_dir(bundle.store_id, root=root)]
    if token == "parent_store":
        # A release that completes its own Store reads its published Store.
        source_id = bundle.store_id if bundle.completes_own_store else bundle.derived_from
        return [paths.store_path(source_id, root=root)]  # type: ignore[arg-type]
    if token == "besd_prefix":
        return [_besd_prefix(bundle)]
    if token == "besd_files":
        prefix = _besd_prefix(bundle)
        return [Path(f"{prefix}{suffix}") for suffix in (".esi", ".epi", ".besd")]
    raise ValueError(f"Unknown path token {token!r}")


def _post_steps(store_p: Path, post: dict[str, Any], spec: CommandSpec) -> list[Step]:
    """Construct post-build steps, rejecting any the subcommand cannot support.

    Keys the Build Recipe omits fall back to `POST_DEFAULTS`; an explicit value
    overrides the default (issue #128).
    """
    post = {**POST_DEFAULTS, **(post or {})}
    steps: list[Step] = []

    if post.get("top_hits") or post.get("top-hits"):
        if spec.top_hits_cmd is None:
            raise ValueError(
                f"top_hits post-processing is not supported or built inline "
                f"for {spec.name} layout"
            )
        steps.append(
            Step(
                name="top-hits",
                argv=["opengwasdb", spec.top_hits_cmd, str(store_p)],
                inputs=[store_p],
                outputs=[store_p],
            )
        )

    if post.get("rho"):
        if spec.rho_cmd is None:
            raise ValueError(
                f"rho post-processing is valid only for dense layout, not {spec.name!r}"
            )
        steps.append(
            Step(
                name="rho",
                argv=["opengwasdb", spec.rho_cmd, str(store_p)],
                inputs=[store_p],
                outputs=[store_p],
            )
        )

    if post.get("overview"):
        if spec.overview_cmd is None:
            raise ValueError(
                f"overview post-processing is not supported for {spec.name} layout"
            )
        steps.append(
            Step(
                name="overview",
                argv=["opengwasdb", spec.overview_cmd, str(store_p)],
                inputs=[store_p],
                outputs=[store_p],
            )
        )

    if post.get("validate"):
        # `--format json` is the machine-readable verdict `register` records
        # (opengwasdb#175); the text form would leave it searching prose (#195).
        steps.append(
            Step(
                name="validate",
                argv=["opengwasdb", "validate", str(store_p), "--format", "json"],
                inputs=[store_p],
                outputs=[],
            )
        )

    return steps


def _variant_reference_declaration(
    phase_block: Mapping[str, Any],
    layout: str,
    command: str,
    options: Mapping[str, Any],
    store_id: str,
) -> tuple[Path, dict[str, Any]] | None:
    """Parse an optional `variant_reference` pre-build declaration.

    Returns the declared destination path and its verbatim-rendered extract
    options, or None when the Build Recipe does not declare a pre-stage. The
    declaration is only valid for a Dense-VCF build on a Dense layout or a
    Hybrid build on a Hybrid layout, and its destination must equal the path the
    build step passes as `--variant-reference`; a mismatch is a recipe error
    rather than a second, silently divergent source of truth (#145/#147/#148).
    """
    raw: Any = None
    for block_key in VARIANT_REFERENCE_BLOCK_KEYS:
        if block_key in phase_block:
            raw = phase_block[block_key]
            break
    if raw is None:
        return None

    supported_command = VARIANT_REFERENCE_LAYOUT_COMMANDS.get(str(layout))
    if supported_command != command:
        raise ValueError(
            f"Bundle {store_id}: a variant_reference pre-build stage is declared for "
            f"layout {layout!r} command {command!r}, which does not accept "
            f"--variant-reference. Supported layout/command pairs: "
            f"{sorted(VARIANT_REFERENCE_LAYOUT_COMMANDS.items())}"
        )

    extract_options: dict[str, Any] = {}
    if isinstance(raw, str):
        declared_output = raw
    elif isinstance(raw, Mapping):
        present = [(key, raw[key]) for key in VARIANT_REFERENCE_OUTPUT_KEYS if key in raw]
        if not present:
            raise ValueError(
                f"Bundle {store_id}: variant_reference must name its destination with "
                f"one of {list(VARIANT_REFERENCE_OUTPUT_KEYS)}"
            )
        declared_output = present[0][1]
        if any(value != declared_output for _, value in present[1:]):
            raise ValueError(
                f"Bundle {store_id}: variant_reference declares conflicting destination "
                f"paths: {present}"
            )
        raw_options = raw.get("options")
        if raw_options is not None:
            if not isinstance(raw_options, Mapping):
                raise ValueError(
                    f"Bundle {store_id}: variant_reference 'options' must be a mapping"
                )
            extract_options = dict(raw_options)
    else:
        raise ValueError(
            f"Bundle {store_id}: variant_reference must be a path string or a mapping, "
            f"not {type(raw).__name__}"
        )

    if not isinstance(declared_output, str) or not declared_output.strip():
        raise ValueError(
            f"Bundle {store_id}: variant_reference destination must be a non-empty string"
        )

    option_value = options.get("variant-reference")
    if not isinstance(option_value, str) or not option_value.strip():
        raise ValueError(
            f"Bundle {store_id}: variant_reference is declared but build.options does "
            f"not declare a 'variant-reference' path"
        )
    if Path(declared_output) != Path(option_value):
        raise ValueError(
            f"Bundle {store_id}: variant_reference output {declared_output!r} does not "
            f"match build.options 'variant-reference' {option_value!r}"
        )

    return Path(declared_output), extract_options


def indexed_subset_declarations(bundle: Bundle) -> list[IndexedSubsetDeclaration]:
    """Parse and validate optional `indexed_subsets` declarations (pure).

    Absence or an empty list returns `[]`, so a recipe with no declaration
    plans exactly as it did before #209. A Ragged layout has no Dense Component
    and rejects any declaration rather than silently dropping it.
    """
    if INDEXED_SUBSETS_KEY not in bundle.build:
        return []
    raw = bundle.build[INDEXED_SUBSETS_KEY]
    if raw is None:
        raise ValueError(
            f"Bundle {bundle.store_id}: build.yaml {INDEXED_SUBSETS_KEY} must be a "
            "list of declarations, not null; omit the key to declare none"
        )
    if not isinstance(raw, list):
        raise ValueError(
            f"Bundle {bundle.store_id}: build.yaml {INDEXED_SUBSETS_KEY} must be a "
            f"list of declarations, not {type(raw).__name__}"
        )
    if not raw:
        return []

    layout = bundle.layout
    if layout not in INDEXED_SUBSET_LAYOUTS:
        raise ValueError(
            f"Bundle {bundle.store_id}: {INDEXED_SUBSETS_KEY} may be declared only "
            f"for {sorted(INDEXED_SUBSET_LAYOUTS)} layout, not {layout!r}; a Ragged "
            "Store Release has no Dense Component to index"
        )

    declarations: list[IndexedSubsetDeclaration] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"Bundle {bundle.store_id}: build.yaml {INDEXED_SUBSETS_KEY}[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{where} must be a mapping")
        for key in INDEXED_SUBSET_REQUIRED_KEYS:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where} requires a non-empty string {key!r}")
        name = str(entry["name"])
        if not is_valid_indexed_subset_name(name):
            raise ValueError(
                f"{where} name {name!r} must match "
                f"{INDEXED_SUBSET_NAME_RE.pattern} and contain no '..'"
            )
        if name in seen:
            raise ValueError(f"{where} duplicates subset name {name!r}")
        seen.add(name)
        checksum = entry.get(INDEXED_SUBSET_CHECKSUM_KEY)
        if checksum is not None and not (
            isinstance(checksum, str) and _SHA256_RE.fullmatch(checksum)
        ):
            raise ValueError(f"{where} checksum must be a 64-character hex sha256")
        options = entry.get(INDEXED_SUBSET_OPTIONS_KEY)
        if options is not None and not isinstance(options, Mapping):
            raise ValueError(f"{where} options must be a mapping")
        if isinstance(options, Mapping):
            reserved = reserved_options(options, INDEXED_SUBSET_RESERVED_OPTION_KEYS)
            if reserved:
                raise ValueError(
                    f"{where} options must not set {reserved}; variant_list and "
                    "reference_assembly are the declaration's own verified inputs"
                )
        declarations.append(
            IndexedSubsetDeclaration(
                name=name,
                resource_id=str(entry["resource_id"]),
                variant_list=Path(str(entry["variant_list"])),
                reference_assembly=str(entry["reference_assembly"]),
                checksum=str(checksum) if checksum is not None else None,
                options=dict(options) if isinstance(options, Mapping) else {},
            )
        )
    return declarations


def _indexed_subset_steps(
    store_p: Path, declarations: list[IndexedSubsetDeclaration]
) -> list[Step]:
    """One `build-indexed-subset` Step per declaration, in declaration order."""
    steps: list[Step] = []
    for declaration in declarations:
        argv = [
            "opengwasdb",
            INDEXED_SUBSET_COMMAND,
            str(store_p),
            declaration.name,
            "--variant-list",
            str(declaration.variant_list),
            "--reference-assembly",
            declaration.reference_assembly,
        ]
        argv.extend(render_options(declaration.options))
        steps.append(
            Step(
                name=indexed_subset_step_name(declaration.name),
                argv=argv,
                inputs=[store_p, declaration.variant_list],
                outputs=[store_p],
            )
        )
    return steps


def _insert_subset_steps(post_steps: list[Step], subset_steps: list[Step]) -> list[Step]:
    """Place declared subset steps after the Store's inline index/rho steps.

    They go before overview and validation, so overview reports them and
    validate audits them (stores #209 acceptance criteria).
    """
    insert_at = next(
        (i for i, s in enumerate(post_steps) if s.name in ("overview", "validate")),
        len(post_steps),
    )
    return [*post_steps[:insert_at], *subset_steps, *post_steps[insert_at:]]


def _select_spec(bundle: Bundle, key: tuple[str, str]) -> tuple[str, CommandSpec]:
    """Resolve the declared subcommand and its spec, or raise NotImplementedError."""
    phase = "complete" if bundle.completion_state == "reference_completed" else "build"
    block = bundle.build.get(phase) or {}
    command = block.get("command", DEFAULT_COMMANDS[key])

    spec = COMMANDS.get(command)
    if spec is None or spec.phase != phase:
        noun = "completion" if phase == "complete" else "build"
        supported = sorted(c for c, s in COMMANDS.items() if s.phase == phase)
        raise NotImplementedError(
            f"Unsupported {bundle.layout} {noun} command {command!r}. "
            f"Configured commands: {supported}"
        )
    return command, spec


def plan(
    bundle: Bundle,
    artifact_root: Path | str | None = None,
) -> list[Step]:
    """Generate the sequence of Steps needed to build a Store Release.

    A pure function: reads bundle metadata and returns a list of Step objects
    holding exact opengwasdb argv and explicit inputs/outputs. `artifact_root`
    is the deployment-resolved root from `paths.artifact_root()`; the Build
    Recipe is not consulted for it (issue #126). Performs no I/O beyond path
    construction.

    A release that completes its own Store (`Bundle.completes_own_store`)
    returns its build phase, then its completion phase (ADR 0037); each
    Step's `phase` says which.
    """
    if bundle.completes_own_store:
        return _plan_completing_own_store(bundle, artifact_root)
    if not bundle.layout or not bundle.completion_state:
        raise ValueError(
            f"Bundle {bundle.store_id} missing layout or completion_state in build.yaml"
        )
    key = (bundle.layout, bundle.completion_state)
    if key not in DEFAULT_COMMANDS:
        raise NotImplementedError(
            f"Unsupported layout and completion_state: {key!r}. "
            f"Configured dispatch entries: {sorted(DEFAULT_COMMANDS)}"
        )

    paths.require_valid_store_id(bundle.store_id)
    command, spec = _select_spec(bundle, key)

    if spec.phase == "complete":
        # A two-id child: a completion without `derived_from` completes its
        # own Store and was planned above.
        paths.require_valid_store_id(bundle.derived_from)  # type: ignore[arg-type]

    root = _resolve_artifact_root(artifact_root)
    phase_block = bundle.build.get(spec.phase) or {}
    options = phase_block.get("options") or {}
    store_p = paths.store_path(bundle.store_id, root=root)

    argv = ["opengwasdb", command]
    for token in spec.positionals:
        argv.extend(str(p) for p in _resolve(token, bundle, root))
    if spec.identity:
        argv.extend(["--store-id", bundle.store_id])
    argv.extend(["--release-id", bundle.store_id])
    if spec.analyses_flag:
        argv.extend(["--analyses", str(paths.build_manifest_path(bundle.store_id, root=root))])
    argv.extend(render_options(options))

    inputs: list[Path] = []
    for token in spec.inputs:
        inputs.extend(_resolve(token, bundle, root))

    steps: list[Step] = []
    declaration = _variant_reference_declaration(
        phase_block, bundle.layout or "", command, options, bundle.store_id
    )
    if declaration is not None:
        ref_output, ref_options = declaration
        manifest_p = paths.build_manifest_path(bundle.store_id, root=root)
        ref_argv = [
            "opengwasdb",
            "extract-variant-reference",
            str(manifest_p),
            "--output-path",
            str(ref_output),
        ]
        ref_argv.extend(render_options(ref_options))
        steps.append(
            Step(
                name="variant-reference",
                argv=ref_argv,
                inputs=[manifest_p],
                outputs=[ref_output],
            )
        )
        # The build step consumes the artifact the pre-stage produces, so the
        # planned Step graph shows the dependency (acceptance criterion #147).
        inputs.append(ref_output)

    steps.append(Step(name=spec.phase, argv=argv, inputs=inputs, outputs=[store_p]))
    # Parse declarations before post-step validation so a Ragged recipe's
    # declaration is refused for the reason that matters (#209).
    subset_declarations = indexed_subset_declarations(bundle)
    post_steps = _post_steps(store_p, bundle.build.get("post") or {}, spec)
    subset_steps = _indexed_subset_steps(store_p, subset_declarations)
    steps.extend(_insert_subset_steps(post_steps, subset_steps))
    return steps


def _plan_completing_own_store(bundle: Bundle, artifact_root: Path | str | None) -> list[Step]:
    """Both phases of a release that completes its own Store under its id (ADR 0037).

    The build phase is exactly what the same bundle plans as `observed_only`
    without its `complete` block, so a Store built and published before the
    bundle gained that block is never rebuilt to complete it (#305). The
    completion phase follows it.
    """
    if bundle.layout not in SINGLE_ID_COMPLETION_LAYOUTS:
        raise ValueError(
            f"Bundle {bundle.store_id}: a reference_completed release with no derived_from "
            f"completes its own Store, which is planned only for "
            f"{sorted(SINGLE_ID_COMPLETION_LAYOUTS)} layout, not {bundle.layout!r}: the "
            "Observed-View Check that gates the replacement compares no other layout yet "
            "(ADR 0037)"
        )
    if not isinstance(bundle.build.get("build"), Mapping):
        raise ValueError(
            f"Bundle {bundle.store_id}: a reference_completed release with no derived_from "
            "must declare a 'build' block: its build phase builds and publishes the "
            "Observed-Only Store that its completion phase completes (ADR 0037)"
        )
    observed_only = replace(
        bundle,
        build={
            **{key: value for key, value in bundle.build.items() if key != "complete"},
            "completion_state": "observed_only",
        },
    )
    build_phase = plan(observed_only, artifact_root=artifact_root)
    return build_phase + _completion_phase(bundle, _resolve_artifact_root(artifact_root))


def _completion_phase(bundle: Bundle, root: Path) -> list[Step]:
    """`complete` from the id's published Store into its staged Store, then the post steps.

    `complete` names the same canonical Store path twice, as its source and
    its destination; `published_store_arg` marks the source, and staging
    rewrites only the destination. The declared post steps then run on the
    staged Store under completion-phase names. Every completion command
    builds the Top-Hit Index inline, so a declared `top_hits` is met by
    `complete` itself and plans no step of its own. The Observed-View Check
    comes last, given the `complete` options its command's `check_options`
    names.
    """
    command, spec = _select_spec(bundle, (bundle.layout, bundle.completion_state))
    block = bundle.build.get("complete") or {}
    options = block.get("options") or {}
    # Refuses any variant_reference pre-stage: no completion command takes one.
    _variant_reference_declaration(block, bundle.layout or "", command, options, bundle.store_id)
    store_p = paths.store_path(bundle.store_id, root=root)

    argv = ["opengwasdb", command]
    published_store_arg: int | None = None
    for token in spec.positionals:
        if token == "parent_store":
            published_store_arg = len(argv)
        argv.extend(str(p) for p in _resolve(token, bundle, root))
    argv.extend(["--release-id", f"{bundle.store_id}{COMPLETED_RELEASE_SUFFIX}"])
    argv.extend(render_options(options))
    inputs = [p for token in spec.inputs for p in _resolve(token, bundle, root)]
    complete = Step(
        name=spec.phase,
        argv=argv,
        inputs=inputs,
        outputs=[store_p],
        phase=COMPLETION_PHASE,
        published_store_arg=published_store_arg,
    )

    check_options = _observed_view_check_options(bundle, command, spec, options)
    post = {
        key: value
        for key, value in (bundle.build.get("post") or {}).items()
        if key not in ("top_hits", "top-hits")
    }
    post_steps = _insert_subset_steps(
        _post_steps(store_p, post, spec),
        _indexed_subset_steps(store_p, indexed_subset_declarations(bundle)),
    )
    check = Step(
        name=OBSERVED_VIEW_CHECK_STEP,
        argv=[
            "opengwasdb",
            OBSERVED_VIEW_CHECK_COMMAND,
            str(store_p),
            str(store_p),
            "--seed",
            str(OBSERVED_VIEW_CHECK_SEED),
            "--n-random",
            str(OBSERVED_VIEW_CHECK_N_RANDOM),
            *check_options,
            "--output",
            str(paths.work_dir(bundle.store_id, root=root) / OBSERVED_VIEW_VERDICT_FILENAME),
        ],
        inputs=[store_p],
        outputs=[],
        phase=COMPLETION_PHASE,
        published_store_arg=2,
    )
    return [
        complete,
        *(
            replace(step, name=completion_step_name(step.name), phase=COMPLETION_PHASE)
            for step in post_steps
        ),
        check,
    ]


def _observed_view_check_options(
    bundle: Bundle, command: str, spec: CommandSpec, options: Mapping[Any, Any]
) -> list[str]:
    """The `check_options` of `command`, rendered from its `complete` options as `complete` renders them.

    Each must be declared with a value (see `COMMANDS`), so the check that
    gates the swap is never planned without one.
    """
    declared = {normalize_option_key(key): key for key in options}
    missing = [name for name in spec.check_options if options.get(declared.get(name)) in (None, "")]
    if missing:
        raise ValueError(
            f"Bundle {bundle.store_id}: {command} completes its own Store, so its 'complete' block "
            f"must declare {', '.join(repr(name) for name in spec.check_options)}, which the "
            f"Observed-View Check that gates the replacement is given too; it declares no "
            f"{', '.join(repr(name) for name in missing)} option (ADR 0037)"
        )
    return render_options({declared[name]: options[declared[name]] for name in spec.check_options})


__all__ = [
    "BUILD_PHASE",
    "COMMANDS",
    "COMPLETED_RELEASE_SUFFIX",
    "COMPLETION_PHASE",
    "COMPLETION_STEP_PREFIX",
    "DEFAULT_COMMANDS",
    "INDEXED_SUBSET_COMMAND",
    "OBSERVED_VIEW_CHECK_COMMAND",
    "OBSERVED_VIEW_CHECK_N_RANDOM",
    "OBSERVED_VIEW_CHECK_SEED",
    "OBSERVED_VIEW_CHECK_STEP",
    "OBSERVED_VIEW_VERDICT_FILENAME",
    "POST_DEFAULTS",
    "REGISTER_STEPS",
    "SINGLE_ID_COMPLETION_LAYOUTS",
    "VARIANT_REFERENCE_BLOCK_KEYS",
    "VARIANT_REFERENCE_COMMANDS",
    "VARIANT_REFERENCE_LAYOUT_COMMANDS",
    "VARIANT_REFERENCE_OUTPUT_KEYS",
    "CommandSpec",
    "IndexedSubsetDeclaration",
    "Step",
    "completion_step_name",
    "indexed_subset_declarations",
    "plan",
    "release_register",
    "render_options",
]
