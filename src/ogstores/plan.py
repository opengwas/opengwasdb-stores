"""The seam: a Release Bundle in, a sequence of `opengwasdb` argv out.

    plan(bundle) -> [Step(name, argv, inputs, outputs)]

A pure function. It reads `build.yaml` and `release.yaml`; it opens no Store,
reads no `analyses.tsv` row, and performs no I/O beyond path construction, so
it is deterministic and testable by string comparison.

Dispatch is a table keyed on `(layout, completion_state)`. `build.options`
keys are `opengwasdb` flag names rendered verbatim -- this module never
interprets one (ADR 0023). The only arguments it composes are the registry's
own facts: Store identity, the bundle's `analyses.tsv` path, artifact paths.

This is the only place in the repository that knows how to invoke
`opengwasdb`, which is why the Snakefile's rules and the master list's
`build_command` are two renderings of one thing and cannot disagree.

See docs/spec/store-release-workflow.md and ADRs 0022, 0023, 0024.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ogstores import paths
from ogstores.bundle import Bundle


@dataclass(frozen=True)
class Step:
    """One executable opengwasdb step with explicit argv, inputs, and outputs."""

    name: str
    argv: list[str]
    inputs: list[Path]
    outputs: list[Path]


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
            if value:
                argv.append(flag)
            else:
                argv.append(f"--no-{raw_key}")
        elif isinstance(value, (list, tuple)):
            for item in value:
                argv.extend([flag, str(item)])
        else:
            argv.extend([flag, str(value)])
    return argv


def _resolve_artifact_root(
    bundle: Bundle, artifact_root: Path | str | None = None
) -> Path:
    """Determine the artifact root for path construction."""
    if artifact_root is not None:
        return Path(artifact_root)
    build_artifacts = bundle.build.get("artifacts")
    if isinstance(build_artifacts, dict) and "root" in build_artifacts:
        return Path(build_artifacts["root"])
    return paths.DEFAULT_ARTIFACT_ROOT


def _build_post_steps(
    store_p: Path,
    post: dict[str, Any],
    *,
    top_hits_cmd: str | None = None,
    rho_cmd: str | None = None,
    overview_cmd: str | None = "regenerate-overview",
    validate_cmd: str | None = "validate",
    layout_name: str = "layout",
) -> list[Step]:
    """Construct post-build steps according to planner-specific commands and policy.

    A shared mechanism that receives planner-specific commands/policy without layout
    branching in shared orchestration code.
    """
    steps: list[Step] = []

    # 1. top-hits
    if post.get("top_hits") or post.get("top-hits"):
        if top_hits_cmd is None:
            raise ValueError(
                f"top_hits post-processing is not supported or built inline for {layout_name} layout"
            )
        steps.append(
            Step(
                name="top-hits",
                argv=["opengwasdb", top_hits_cmd, str(store_p)],
                inputs=[store_p],
                outputs=[store_p],
            )
        )

    # 2. rho (dense only)
    if post.get("rho"):
        if rho_cmd is None:
            raise ValueError(
                f"rho post-processing is valid only for dense layout, not {layout_name!r}"
            )
        steps.append(
            Step(
                name="rho",
                argv=["opengwasdb", rho_cmd, str(store_p)],
                inputs=[store_p],
                outputs=[store_p],
            )
        )

    # 3. overview
    if post.get("overview"):
        if overview_cmd is not None:
            steps.append(
                Step(
                    name="overview",
                    argv=["opengwasdb", overview_cmd, str(store_p)],
                    inputs=[store_p],
                    outputs=[store_p],
                )
            )

    # 4. validate
    if post.get("validate"):
        if validate_cmd is not None:
            steps.append(
                Step(
                    name="validate",
                    argv=["opengwasdb", validate_cmd, str(store_p)],
                    inputs=[store_p],
                    outputs=[],
                )
            )

    return steps


def _plan_dense_observed(bundle: Bundle, artifact_root: Path) -> list[Step]:
    """Plan steps for a Dense Observed-Only store release (e.g. OGS-00003)."""
    paths.require_valid_store_id(bundle.store_id)
    if not bundle.family:
        raise ValueError(f"Bundle {bundle.store_id} missing required 'family' in release.yaml")

    store_p = paths.store_path(bundle.store_id, root=artifact_root)
    build_block = bundle.build.get("build") or {}
    command = build_block.get("command", "build-dense-vcf")
    options = build_block.get("options") or {}

    build_argv = [
        "opengwasdb",
        command,
        str(bundle.analyses_path),
        str(store_p),
        "--store-id",
        bundle.family,
        "--release-id",
        bundle.store_id,
        *render_options(options),
    ]

    steps: list[Step] = [
        Step(
            name="build",
            argv=build_argv,
            inputs=[bundle.analyses_path],
            outputs=[store_p],
        )
    ]

    post = bundle.build.get("post") or {}
    steps.extend(
        _build_post_steps(
            store_p,
            post,
            top_hits_cmd="build-dense-top-hits",
            rho_cmd="build-dense-rho",
            overview_cmd="regenerate-overview",
            validate_cmd="validate",
            layout_name="dense",
        )
    )
    return steps


PlannerFn = Callable[[Bundle, Path], list[Step]]


def _plan_hybrid_observed(bundle: Bundle, artifact_root: Path) -> list[Step]:
    """Plan steps for a Hybrid Observed-Only store release (e.g. OGS-00004, OGS-00005)."""
    paths.require_valid_store_id(bundle.store_id)
    if not bundle.family:
        raise ValueError(f"Bundle {bundle.store_id} missing required 'family' in release.yaml")

    store_p = paths.store_path(bundle.store_id, root=artifact_root)
    build_block = bundle.build.get("build") or {}
    command = build_block.get("command", "build-hybrid")
    options = build_block.get("options") or {}

    build_argv = [
        "opengwasdb",
        command,
        str(bundle.analyses_path),
        str(store_p),
        "--store-id",
        bundle.family,
        "--release-id",
        bundle.store_id,
        *render_options(options),
    ]

    steps: list[Step] = [
        Step(
            name="build",
            argv=build_argv,
            inputs=[bundle.analyses_path],
            outputs=[store_p],
        )
    ]

    post = bundle.build.get("post") or {}
    steps.extend(
        _build_post_steps(
            store_p,
            post,
            top_hits_cmd=None,  # Hybrid builds top-hit indexes inline during build-hybrid
            rho_cmd=None,       # rho is dense-only
            overview_cmd="regenerate-overview",
            validate_cmd="validate",
            layout_name="hybrid",
        )
    )
    return steps


@dataclass(frozen=True)
class RaggedBuilderSpec:
    """Specification for a ragged build subcommand and its post-step capabilities."""

    build_fn: Callable[[Bundle, Path, Path, str, dict[str, Any]], Step]
    top_hits_cmd: str | None
    name: str


def _build_step_ragged_ssf(
    bundle: Bundle,
    store_p: Path,
    source_p: Path,
    command: str,
    options: dict[str, Any],
) -> Step:
    """Build step for a Ragged SSF Observed-Only store release (e.g. OGS-00006, OGS-00007)."""
    build_argv = [
        "opengwasdb",
        command,
        str(bundle.analyses_path),
        str(source_p),
        str(store_p),
        "--store-id",
        bundle.family,  # type: ignore[arg-type]
        "--release-id",
        bundle.store_id,
        *render_options(options),
    ]
    return Step(
        name="build",
        argv=build_argv,
        inputs=[bundle.analyses_path],
        outputs=[store_p],
    )


def _build_step_ragged_besd(
    bundle: Bundle,
    store_p: Path,
    source_p: Path,
    command: str,
    options: dict[str, Any],
) -> Step:
    """Build step for a Ragged BESD Observed-Only store release (e.g. OGS-00001)."""
    if not bundle.label:
        raise ValueError(f"Bundle {bundle.store_id} missing required 'label' in release.yaml")

    besd_prefix = source_p / bundle.label
    build_argv = [
        "opengwasdb",
        command,
        str(besd_prefix),
        str(store_p),
        "--store-id",
        bundle.family,  # type: ignore[arg-type]
        "--release-id",
        bundle.store_id,
        "--analyses",
        str(bundle.analyses_path),
        *render_options(options),
    ]
    return Step(
        name="build",
        argv=build_argv,
        inputs=[
            Path(f"{besd_prefix}.esi"),
            Path(f"{besd_prefix}.epi"),
            Path(f"{besd_prefix}.besd"),
            bundle.analyses_path,
        ],
        outputs=[store_p],
    )


RAGGED_BUILD_DISPATCH: dict[str, RaggedBuilderSpec] = {
    "build-ragged-ssf": RaggedBuilderSpec(
        build_fn=_build_step_ragged_ssf,
        top_hits_cmd="build-ragged-top-hits",
        name="ragged-ssf",
    ),
    "build-ragged-besd": RaggedBuilderSpec(
        build_fn=_build_step_ragged_besd,
        top_hits_cmd=None,  # BESD builds top-hit indexes inline during build-ragged-besd
        name="ragged-besd",
    ),
}


def _plan_ragged_observed(bundle: Bundle, artifact_root: Path) -> list[Step]:
    """Plan steps for a Ragged Observed-Only store release (e.g. OGS-00001, OGS-00006, OGS-00007)."""
    paths.require_valid_store_id(bundle.store_id)
    if not bundle.family:
        raise ValueError(f"Bundle {bundle.store_id} missing required 'family' in release.yaml")

    store_p = paths.store_path(bundle.store_id, root=artifact_root)
    source_p = paths.source_dir(bundle.store_id, root=artifact_root)
    build_block = bundle.build.get("build") or {}
    command = build_block.get("command", "build-ragged-ssf")
    options = build_block.get("options") or {}

    spec = RAGGED_BUILD_DISPATCH.get(command)
    if spec is None:
        raise NotImplementedError(
            f"Unsupported ragged build command {command!r}. "
            f"Configured commands: {sorted(RAGGED_BUILD_DISPATCH.keys())}"
        )

    build_step = spec.build_fn(bundle, store_p, source_p, command, options)

    steps: list[Step] = [build_step]

    post = bundle.build.get("post") or {}
    steps.extend(
        _build_post_steps(
            store_p,
            post,
            top_hits_cmd=spec.top_hits_cmd,
            rho_cmd=None,       # rho is dense-only
            overview_cmd="regenerate-overview",
            validate_cmd="validate",
            layout_name=spec.name,
        )
    )
    return steps


def _plan_dense_completed(bundle: Bundle, artifact_root: Path) -> list[Step]:
    """Plan steps for a Dense Reference-Completed store release."""
    paths.require_valid_store_id(bundle.store_id)
    if not bundle.derived_from:
        raise ValueError(
            f"Bundle {bundle.store_id} missing required 'derived_from' in release.yaml"
        )
    paths.require_valid_store_id(bundle.derived_from)

    parent_store_p = paths.store_path(bundle.derived_from, root=artifact_root)
    store_p = paths.store_path(bundle.store_id, root=artifact_root)

    complete_block = bundle.build.get("complete") or {}
    command = complete_block.get("command", "complete-dense")
    options = complete_block.get("options") or {}

    if command != "complete-dense":
        raise NotImplementedError(
            f"Unsupported dense completion command {command!r}; expected 'complete-dense'"
        )

    complete_argv = [
        "opengwasdb",
        command,
        str(parent_store_p),
        str(store_p),
        "--release-id",
        bundle.store_id,
        *render_options(options),
    ]

    steps: list[Step] = [
        Step(
            name="complete",
            argv=complete_argv,
            inputs=[parent_store_p],
            outputs=[store_p],
        )
    ]

    post = bundle.build.get("post") or {}
    steps.extend(
        _build_post_steps(
            store_p,
            post,
            top_hits_cmd=None,  # complete-dense builds top-hit indexes inline
            rho_cmd="build-dense-rho",
            overview_cmd="regenerate-overview",
            validate_cmd="validate",
            layout_name="dense-completed",
        )
    )
    return steps


def _plan_hybrid_completed(bundle: Bundle, artifact_root: Path) -> list[Step]:
    """Plan steps for a Hybrid Reference-Completed store release."""
    paths.require_valid_store_id(bundle.store_id)
    if not bundle.derived_from:
        raise ValueError(
            f"Bundle {bundle.store_id} missing required 'derived_from' in release.yaml"
        )
    paths.require_valid_store_id(bundle.derived_from)

    parent_store_p = paths.store_path(bundle.derived_from, root=artifact_root)
    store_p = paths.store_path(bundle.store_id, root=artifact_root)

    complete_block = bundle.build.get("complete") or {}
    command = complete_block.get("command", "complete-hybrid")
    options = complete_block.get("options") or {}

    if command != "complete-hybrid":
        raise NotImplementedError(
            f"Unsupported hybrid completion command {command!r}; expected 'complete-hybrid'"
        )

    complete_argv = [
        "opengwasdb",
        command,
        str(parent_store_p),
        str(store_p),
        "--release-id",
        bundle.store_id,
        *render_options(options),
    ]

    steps: list[Step] = [
        Step(
            name="complete",
            argv=complete_argv,
            inputs=[parent_store_p],
            outputs=[store_p],
        )
    ]

    post = bundle.build.get("post") or {}
    steps.extend(
        _build_post_steps(
            store_p,
            post,
            top_hits_cmd=None,  # complete-hybrid builds top-hit indexes inline
            rho_cmd=None,       # rho is dense-only
            overview_cmd="regenerate-overview",
            validate_cmd="validate",
            layout_name="hybrid-completed",
        )
    )
    return steps


def _plan_ragged_completed(bundle: Bundle, artifact_root: Path) -> list[Step]:
    """Plan steps for a Ragged Reference-Completed store release (e.g. OGS-00002)."""
    paths.require_valid_store_id(bundle.store_id)
    if not bundle.derived_from:
        raise ValueError(
            f"Bundle {bundle.store_id} missing required 'derived_from' in release.yaml"
        )
    paths.require_valid_store_id(bundle.derived_from)

    parent_store_p = paths.store_path(bundle.derived_from, root=artifact_root)
    store_p = paths.store_path(bundle.store_id, root=artifact_root)

    complete_block = bundle.build.get("complete") or {}
    command = complete_block.get("command", "complete-ragged")
    options = complete_block.get("options") or {}

    if command != "complete-ragged":
        raise NotImplementedError(
            f"Unsupported ragged completion command {command!r}; expected 'complete-ragged'"
        )

    complete_argv = [
        "opengwasdb",
        command,
        str(parent_store_p),
        str(store_p),
        "--release-id",
        bundle.store_id,
        *render_options(options),
    ]

    steps: list[Step] = [
        Step(
            name="complete",
            argv=complete_argv,
            inputs=[parent_store_p],
            outputs=[store_p],
        )
    ]

    post = bundle.build.get("post") or {}
    steps.extend(
        _build_post_steps(
            store_p,
            post,
            top_hits_cmd=None,  # complete-ragged builds top-hit indexes inline
            rho_cmd=None,       # rho is dense-only
            overview_cmd="regenerate-overview",
            validate_cmd="validate",
            layout_name="ragged-completed",
        )
    )
    return steps


DISPATCH_TABLE: dict[tuple[str, str], PlannerFn] = {
    ("dense", "observed_only"): _plan_dense_observed,
    ("hybrid", "observed_only"): _plan_hybrid_observed,
    ("ragged", "observed_only"): _plan_ragged_observed,
    ("dense", "reference_completed"): _plan_dense_completed,
    ("hybrid", "reference_completed"): _plan_hybrid_completed,
    ("ragged", "reference_completed"): _plan_ragged_completed,
}


def plan(
    bundle: Bundle,
    artifact_root: Path | str | None = None,
) -> list[Step]:
    """Generate the sequence of Steps needed to build a Store Release.

    A pure function: reads bundle metadata and returns a list of Step objects
    holding exact opengwasdb argv and explicit inputs/outputs. Performs no I/O
    beyond path construction.
    """
    if not bundle.layout or not bundle.completion_state:
        raise ValueError(
            f"Bundle {bundle.store_id} missing layout or completion_state in build.yaml"
        )
    key = (bundle.layout, bundle.completion_state)
    planner = DISPATCH_TABLE.get(key)
    if planner is None:
        raise NotImplementedError(
            f"Unsupported layout and completion_state: {key!r}. "
            f"Configured dispatch entries: {sorted(DISPATCH_TABLE.keys())}"
        )

    resolved_root = _resolve_artifact_root(bundle, artifact_root)
    return planner(bundle, resolved_root)


__all__ = [
    "DISPATCH_TABLE",
    "RAGGED_BUILD_DISPATCH",
    "Step",
    "plan",
    "render_options",
]
