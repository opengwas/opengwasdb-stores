"""Pure phase model for the throwaway Store Release workflow prototype."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ReleasePlan:
    family_id: str
    observed_release_id: str
    source_root: Path
    analyses_path: Path
    reader_capability: str
    build_operation: str
    rho: bool
    completion: bool
    completed_release_id: str | None
    completion_operation: str | None


@dataclass(frozen=True)
class Phase:
    phase_id: str
    depends_on: tuple[str, ...]
    release_id: str
    mutation_mode: str
    expensive: bool = False


@dataclass(frozen=True)
class PrototypePaths:
    repo_root: Path
    state_root: Path
    config_path: Path

    def checkpoint(self, phase_id: str) -> Path:
        return self.state_root / "checkpoints" / f"{phase_id}.json"

    def report(self, release_id: str, name: str) -> Path:
        return self.state_root / "reports" / release_id / name

    def resolved_analyses(self) -> Path:
        return self.state_root / "work" / "analyses.resolved.tsv"

    def store_dir(self, family_id: str, release_id: str) -> Path:
        return self.state_root / "stores" / family_id / "releases" / release_id / "store"

    def release_dir(self, family_id: str, release_id: str) -> Path:
        return self.state_root / "registry" / "families" / family_id / "releases" / release_id


def load_prototype(
    repo_root: Path, config_path: Path
) -> tuple[ReleasePlan, PrototypePaths, dict[str, Any]]:
    config = yaml.safe_load(config_path.read_text())
    source = config["source"]
    release = config["release"]
    completion = config.get("reference_completion", {})
    plan = ReleasePlan(
        family_id=release["store_family_id"],
        observed_release_id=release["family_release_id"],
        source_root=(repo_root / source["root"]).resolve(),
        analyses_path=(repo_root / source["analyses"]).resolve(),
        reader_capability=source["reader"]["capability"],
        build_operation=config["build"]["operation"],
        rho=bool(config.get("rho", {}).get("enabled", False)),
        completion=bool(completion.get("enabled", False)),
        completed_release_id=completion.get("family_release_id"),
        completion_operation=completion.get("operation"),
    )
    paths = PrototypePaths(
        repo_root=repo_root,
        state_root=(repo_root / config["prototype_state"]).resolve(),
        config_path=config_path,
    )
    return plan, paths, config


def source_files(plan: ReleasePlan) -> tuple[Path, ...]:
    with plan.analyses_path.open(newline="") as stream:
        rows = csv.DictReader(stream, delimiter="\t")
        return tuple(plan.source_root / row["file_name"] for row in rows)


def phase_graph(plan: ReleasePlan) -> tuple[Phase, ...]:
    """Return the proposed workflow graph without doing I/O."""
    phases: list[Phase] = [
        Phase("validate_fixed_inputs", (), plan.observed_release_id, "new-output"),
        Phase(
            "resolve_analysis_metadata",
            ("validate_fixed_inputs",),
            plan.observed_release_id,
            "new-output",
        ),
        Phase(
            "build_observed_store",
            ("resolve_analysis_metadata",),
            plan.observed_release_id,
            "new-output",
            expensive=True,
        ),
    ]
    observed_tail = "build_observed_store"
    if plan.rho:
        phases.append(
            Phase(
                "build_observed_rho",
                (observed_tail,),
                plan.observed_release_id,
                "in-place-update",
                expensive=True,
            )
        )
        observed_tail = "build_observed_rho"
    phases.extend(
        [
            Phase(
                "regenerate_observed_overview",
                (observed_tail,),
                plan.observed_release_id,
                "in-place-update",
            ),
            Phase(
                "validate_observed_release",
                ("regenerate_observed_overview",),
                plan.observed_release_id,
                "new-output",
            ),
        ]
    )

    if plan.completion:
        assert plan.completed_release_id
        child = plan.completed_release_id
        phases.extend(
            [
                Phase(
                    "register_completed_release",
                    ("validate_observed_release",),
                    child,
                    "new-output",
                ),
                Phase(
                    "complete_store",
                    ("register_completed_release",),
                    child,
                    "new-output",
                    expensive=True,
                ),
            ]
        )
        completed_tail = "complete_store"
        if plan.rho:
            phases.append(
                Phase(
                    "build_completed_rho",
                    (completed_tail,),
                    child,
                    "in-place-update",
                    expensive=True,
                )
            )
            completed_tail = "build_completed_rho"
        phases.extend(
            [
                Phase(
                    "regenerate_completed_overview",
                    (completed_tail,),
                    child,
                    "in-place-update",
                ),
                Phase(
                    "validate_completed_release",
                    ("regenerate_completed_overview",),
                    child,
                    "new-output",
                ),
            ]
        )
    return tuple(phases)


def final_phase(plan: ReleasePlan) -> str:
    return "validate_completed_release" if plan.completion else "validate_observed_release"
