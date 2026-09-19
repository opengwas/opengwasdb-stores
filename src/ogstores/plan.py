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

This is the only place in the repository that knows how to invoke
`opengwasdb`, which is why the Snakefile's rules and the master list's
`build_command` are two renderings of one thing and cannot disagree.

See docs/spec/store-release-workflow.md and ADRs 0022, 0023, 0024.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ogstores import paths
from ogstores.bundle import Bundle


@dataclass(frozen=True)
class Step:
    """One executable opengwasdb step with explicit argv, inputs, and outputs."""

    name: str
    argv: list[str]
    inputs: list[Path]
    outputs: list[Path]


@dataclass(frozen=True)
class CommandSpec:
    """How one `opengwasdb` subcommand is invoked, and what may follow it.

    `name` is the label used in post-step policy errors. `positionals` and
    `inputs` are drawn from a fixed token vocabulary resolved by `_resolve`.
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
    "complete-ragged": CommandSpec(
        phase="complete",
        name="ragged-completed",
        positionals=("parent_store", "store"),
        inputs=("parent_store",),
        identity=False,
    ),
}

# Build-phase subcommands that accept `--variant-reference`, and therefore may
# be preceded by an `extract-variant-reference` pre-build step (#145/#147).
VARIANT_REFERENCE_COMMANDS: frozenset[str] = frozenset({"build-dense-vcf", "build-hybrid"})

# The optional Build Recipe block that declares the pre-stage, and the keys it
# may use to name the destination path. A bare string is also accepted as
# shorthand for `{output: <string>}`.
VARIANT_REFERENCE_BLOCK_KEYS: tuple[str, ...] = ("variant_reference", "variant-reference")
VARIANT_REFERENCE_OUTPUT_KEYS: tuple[str, ...] = ("output", "path", "output_path", "output-path")

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
        raw_key = str(key).strip().lstrip("-")
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
        return [paths.store_path(bundle.derived_from, root=root)]  # type: ignore[arg-type]
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
        steps.append(
            Step(
                name="validate",
                argv=["opengwasdb", "validate", str(store_p)],
                inputs=[store_p],
                outputs=[],
            )
        )

    return steps


def _variant_reference_declaration(
    phase_block: Mapping[str, Any],
    command: str,
    options: Mapping[str, Any],
    store_id: str,
) -> tuple[Path, dict[str, Any]] | None:
    """Parse an optional `variant_reference` pre-build declaration.

    Returns the declared destination path and its verbatim-rendered extract
    options, or None when the Build Recipe does not declare a pre-stage. The
    declaration is only valid for build commands that accept
    `--variant-reference`, and its destination must equal the path the build
    step passes as `--variant-reference`; a mismatch is a recipe error rather
    than a second, silently divergent source of truth (#145/#147).
    """
    raw: Any = None
    for block_key in VARIANT_REFERENCE_BLOCK_KEYS:
        if block_key in phase_block:
            raw = phase_block[block_key]
            break
    if raw is None:
        return None

    if command not in VARIANT_REFERENCE_COMMANDS:
        raise ValueError(
            f"Bundle {store_id}: a variant_reference pre-build stage is declared for "
            f"command {command!r}, which does not accept --variant-reference. "
            f"Supported commands: {sorted(VARIANT_REFERENCE_COMMANDS)}"
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
    """
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
        if not bundle.derived_from:
            raise ValueError(
                f"Bundle {bundle.store_id} missing required 'derived_from' in release.yaml"
            )
        paths.require_valid_store_id(bundle.derived_from)

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
        phase_block, command, options, bundle.store_id
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
    steps.extend(_post_steps(store_p, bundle.build.get("post") or {}, spec))
    return steps


__all__ = [
    "COMMANDS",
    "DEFAULT_COMMANDS",
    "POST_DEFAULTS",
    "VARIANT_REFERENCE_BLOCK_KEYS",
    "VARIANT_REFERENCE_COMMANDS",
    "VARIANT_REFERENCE_OUTPUT_KEYS",
    "CommandSpec",
    "Step",
    "plan",
    "render_options",
]
