#!/usr/bin/env python3
"""Store Release workflow model (issue #98).

The production Store Release workflow is a Snakemake DAG whose Snakefile is
wiring only: it reads one release's `build.yaml` and declares which phase
depends on which. Everything that knows what a row, a column, or a Store Layout
*means* lives here and in `workflow/phase.py`, on the registry side of the
OpenGWASDB boundary.

This module is the pure part of that model -- the release's paths, its phase
identities, and the fixed inputs every completion record binds. The Snakefile
and the phase runner both import it, so the DAG's declared outputs and the
runner's written outputs cannot drift apart.

Layout under the configured artifact root follows ADR 0018
(`<artifact-root>/<store-family-id>/releases/<family-release-id>/`). Work files
(`work/`) and the built Store are artifacts, not tracked registry metadata
(ADR 0015); the release bundle's `sidecars/` reports and `validation.yaml` are
the only things this workflow writes back into the repository.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from resources.lib.release_manifest import ADAPTER_PROJECTIONS, buildable_rows  # noqa: E402
from resources.lib.release_plan import PlanError, ReleasePlan, load_plan  # noqa: E402
from resources.lib.release_yaml import read_release_yaml, read_tsv  # noqa: E402

#: The phase whose completion record is the workflow's final target.
FINAL_PHASE = "validate_observed_release"

#: Directory under the configured artifact root that holds the built Store.
STORE_DIR_NAME = "store.opengwasdb"

#: Prefix for the sibling directory a build phase writes into before it replaces
#: the Store atomically (see `workflow/README.md`, "Resumption contract").
STORE_PARTIAL_SUFFIX = ".partial"


class WorkflowError(Exception):
    """A Release that cannot drive the production Store Release workflow."""


@dataclass(frozen=True)
class WorkflowPaths:
    """Every path the workflow reads or writes for one Store Release."""

    repo_root: Path
    release_dir: Path
    config_path: Path
    artifact_root: Path
    work_dir: Path
    store_dir: Path

    def completion(self, phase_id: str) -> Path:
        """The phase's completion record: the file Snakemake tracks as its output."""
        return self.work_dir / "completions" / f"{phase_id}.json"

    def report(self, name: str) -> Path:
        """A small phase report, tracked with the release bundle (ADR 0015)."""
        return self.release_dir / "sidecars" / name

    @property
    def resolved_analyses(self) -> Path:
        """The immutable working input the resolve phase emits."""
        return self.work_dir / "analyses.resolved.tsv"

    @property
    def builder_manifest(self) -> Path:
        """The shared builder manifest (issue #96) the build phase feeds OpenGWASDB."""
        return self.work_dir / "builder-manifest.tsv"

    @property
    def store_partial(self) -> Path:
        """Where a build phase writes before replacing the Store atomically."""
        return self.store_dir.with_name(self.store_dir.name + STORE_PARTIAL_SUFFIX)

    @property
    def validation_yaml(self) -> Path:
        return self.release_dir / "validation.yaml"

    @property
    def release_yaml(self) -> Path:
        """The release bundle's lifecycle record (Release Status)."""
        return self.release_dir / "release.yaml"


@dataclass(frozen=True)
class Workflow:
    """One release's loaded plan, resolved paths, and fixed inputs."""

    plan: ReleasePlan
    config: dict
    paths: WorkflowPaths
    layout: str
    source_paths: tuple[Path, ...]
    reference_descriptors: tuple[Path, ...]


def _resolve(root: Path, value: object, *, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{key}: must be a non-empty path")
    path = Path(value)
    return path if path.is_absolute() else root / path


def _mapping(config: dict, key: str) -> dict:
    value = config.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowError(f"{key}: must be a mapping")
    return value


def source_file_paths(plan: ReleasePlan) -> tuple[Path, ...]:
    """The release's selected raw source files, in `analyses.tsv` order.

    Paths are resolved relative to `source.root` unless absolute, exactly as
    `resources/lib/release_plan.py` resolves them when it verifies checksums.
    Rows marked `exclude_from_build` are not read by any phase (issue #96
    `buildable_rows`), so they are not part of the workflow's input identity.
    """
    if plan.analyses_path is None or plan.source_root is None:
        raise WorkflowError("build.yaml does not resolve source.root/source.analyses")
    files: list[Path] = []
    for row in buildable_rows(read_tsv(plan.analyses_path)):
        value = (row.get("source_file") or row.get("file_name") or "").strip()
        if not value:
            raise WorkflowError(f"source.analyses: {row.get('analysis_id') or 'row'} names no source file")
        path = Path(value)
        files.append(path if path.is_absolute() else plan.source_root / path)
    return tuple(files)


def reference_descriptors(repo_root: Path, config: dict) -> tuple[Path, ...]:
    """The declared Reference Resources' descriptors and tracked data, hashed
    into every record.

    A resource's data usually stays external (ADR 0011/0015); its *descriptor*
    is the small `reference-resources/<resource_id>/resource.yaml` declaration,
    which is what a completion record can bind. A declared resource with no
    descriptor in this repository (an external resource recorded only in
    `build.yaml`) is bound through `build.yaml`'s own hash instead.

    When a resource's `location`/`fine_group_map` resolve to a file inside this
    repository (a small tracked panel: the QC panel, the workflow fixture's
    ancestry mixture), that file is bound too, so swapping the panel invalidates
    the phases that used it (issue #99) rather than only its declaration.
    """
    resources = config.get("reference_resources") or []
    if not isinstance(resources, list):
        raise WorkflowError("reference_resources: must be a list")
    descriptors: list[Path] = []
    for entry in resources:
        if not isinstance(entry, dict) or not entry.get("resource_id"):
            continue
        candidate = repo_root / "reference-resources" / str(entry["resource_id"]) / "resource.yaml"
        if candidate.is_file():
            descriptors.append(candidate)
        for key in ("location", "fine_group_map"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            data_path = Path(value)
            data_path = data_path if data_path.is_absolute() else repo_root / data_path
            if data_path.is_file():
                descriptors.append(data_path)
    return tuple(sorted(set(descriptors)))


def load_workflow(config_path: Path, repo_root: Path) -> Workflow:
    """Load one release's `build.yaml` into an executable workflow.

    Refuses a legacy-schema release (the workflow runs `build.command`, and
    issue #97 migrates the checked-in bundles), a release with no artifact root
    to build into, and any release asking for a branch this ticket has not wired
    (rho and Reference Completion are issue #100).
    """
    config_path = Path(config_path).resolve()
    repo_root = Path(repo_root).resolve()
    release_dir = config_path.parent
    try:
        plan = load_plan(release_dir)
    except PlanError as exc:
        raise WorkflowError(str(exc)) from exc

    if plan.schema != "cli":
        raise WorkflowError(
            f"{config_path} is the pre-#95 build.yaml schema; the workflow executes "
            "build.command (CLI schema) only (issue #97 migrates checked-in releases)"
        )
    if plan.rho_enabled:
        raise WorkflowError("rho is not wired yet (issue #100); set rho.enabled: false")
    if plan.reference_completion_enabled:
        raise WorkflowError(
            "Reference Completion is not wired yet (issue #100); "
            "set reference_completion.enabled: false"
        )

    config = read_release_yaml(config_path)
    artifacts = _mapping(config, "artifacts")
    artifact_root = _resolve(repo_root, artifacts.get("artifact_root"), key="artifacts.artifact_root")
    release_subdir = str(
        artifacts.get("release_subdir")
        or f"{plan.store_family_id}/releases/{plan.family_release_id}"
    )
    store_dir = (
        _resolve(repo_root, artifacts["store_uri"], key="artifacts.store_uri")
        if artifacts.get("store_uri")
        else artifact_root / release_subdir / STORE_DIR_NAME
    )
    work_dir = (
        _resolve(repo_root, artifacts["work_dir"], key="artifacts.work_dir")
        if artifacts.get("work_dir")
        else artifact_root / release_subdir / "work"
    )

    layout = (plan.store_layout or "").split("-")[0]
    if layout not in ADAPTER_PROJECTIONS:
        raise WorkflowError(
            f"store_layout {plan.store_layout!r} does not name a Store Layout this workflow "
            f"knows ({', '.join(sorted(ADAPTER_PROJECTIONS))})"
        )

    paths = WorkflowPaths(
        repo_root=repo_root,
        release_dir=release_dir,
        config_path=config_path,
        artifact_root=artifact_root,
        work_dir=work_dir,
        store_dir=store_dir,
    )
    return Workflow(
        plan=plan,
        config=config,
        paths=paths,
        layout=layout,
        source_paths=source_file_paths(plan),
        reference_descriptors=reference_descriptors(repo_root, config),
    )
