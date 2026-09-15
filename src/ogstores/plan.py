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
registry's own facts: Store identity, the bundle's `analyses.tsv` path,
artifact paths.

This is the only place in the repository that knows how to invoke
`opengwasdb`, which is why the Snakefile's rules and the master list's
`build_command` are two renderings of one thing and cannot disagree.

See docs/spec/store-release-workflow.md and ADRs 0022, 0023, 0024.
"""

from __future__ import annotations

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


COMMANDS: dict[str, CommandSpec] = {
    "build-dense-vcf": CommandSpec(
        phase="build",
        name="dense",
        positionals=("analyses", "store"),
        inputs=("analyses",),
        top_hits_cmd="build-dense-top-hits",
        rho_cmd="build-dense-rho",
    ),
    # Hybrid builds top-hit indexes inline during build-hybrid; rho is dense-only.
    "build-hybrid": CommandSpec(
        phase="build",
        name="hybrid",
        positionals=("analyses", "store"),
        inputs=("analyses",),
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
    ),
    "complete-hybrid": CommandSpec(
        phase="complete",
        name="hybrid-completed",
        positionals=("parent_store", "store"),
        inputs=("parent_store",),
        identity=False,
    ),
    "complete-ragged": CommandSpec(
        phase="complete",
        name="ragged-completed",
        positionals=("parent_store", "store"),
        inputs=("parent_store",),
        identity=False,
    ),
}

DEFAULT_COMMANDS: dict[tuple[str, str], str] = {
    ("dense", "observed_only"): "build-dense-vcf",
    ("hybrid", "observed_only"): "build-hybrid",
    ("ragged", "observed_only"): "build-ragged-ssf",
    ("dense", "reference_completed"): "complete-dense",
    ("hybrid", "reference_completed"): "complete-hybrid",
    ("ragged", "reference_completed"): "complete-ragged",
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


def _besd_prefix(bundle: Bundle, root: Path) -> Path:
    """Path prefix shared by a BESD triple, from the bundle's label."""
    if not bundle.label:
        raise ValueError(
            f"Bundle {bundle.store_id} missing required 'label' in release.yaml"
        )
    return paths.source_dir(bundle.store_id, root=root) / bundle.label


def _resolve(token: str, bundle: Bundle, root: Path) -> list[Path]:
    """Resolve one positional/input token to concrete paths."""
    if token == "analyses":
        return [bundle.analyses_path]
    if token == "store":
        return [paths.store_path(bundle.store_id, root=root)]
    if token == "source":
        return [paths.source_dir(bundle.store_id, root=root)]
    if token == "parent_store":
        return [paths.store_path(bundle.derived_from, root=root)]  # type: ignore[arg-type]
    if token == "besd_prefix":
        return [_besd_prefix(bundle, root)]
    if token == "besd_files":
        prefix = _besd_prefix(bundle, root)
        return [Path(f"{prefix}{suffix}") for suffix in (".esi", ".epi", ".besd")]
    raise ValueError(f"Unknown path token {token!r}")


def _post_steps(store_p: Path, post: dict[str, Any], spec: CommandSpec) -> list[Step]:
    """Construct post-build steps, rejecting any the subcommand cannot support."""
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
        steps.append(
            Step(
                name="overview",
                argv=["opengwasdb", "regenerate-overview", str(store_p)],
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
    holding exact opengwasdb argv and explicit inputs/outputs. Performs no I/O
    beyond path construction.
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
    elif not bundle.family:
        raise ValueError(
            f"Bundle {bundle.store_id} missing required 'family' in release.yaml"
        )

    root = _resolve_artifact_root(bundle, artifact_root)
    options = (bundle.build.get(spec.phase) or {}).get("options") or {}
    store_p = paths.store_path(bundle.store_id, root=root)

    argv = ["opengwasdb", command]
    for token in spec.positionals:
        argv.extend(str(p) for p in _resolve(token, bundle, root))
    if spec.identity:
        argv.extend(["--store-id", bundle.family])  # type: ignore[list-item]
    argv.extend(["--release-id", bundle.store_id])
    if spec.analyses_flag:
        argv.extend(["--analyses", str(bundle.analyses_path)])
    argv.extend(render_options(options))

    inputs: list[Path] = []
    for token in spec.inputs:
        inputs.extend(_resolve(token, bundle, root))

    steps = [Step(name=spec.phase, argv=argv, inputs=inputs, outputs=[store_p])]
    steps.extend(_post_steps(store_p, bundle.build.get("post") or {}, spec))
    return steps


__all__ = [
    "COMMANDS",
    "DEFAULT_COMMANDS",
    "CommandSpec",
    "Step",
    "plan",
    "render_options",
]
