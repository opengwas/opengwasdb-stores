#!/usr/bin/env python3
"""Interactive shell for the Snakemake Release Pipeline prototype."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from pipeline_model import load_prototype, phase_graph


BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
CONFIG = HERE / "fixtures" / "build.yaml"
PLAN, PATHS, CONFIGURATION = load_prototype(REPO_ROOT, CONFIG)
LAST_ACTION = "Ready. Run the complete release DAG or inspect its dry-run."


def phase_states() -> dict[str, str]:
    states: dict[str, str] = {}
    for phase in phase_graph(PLAN):
        output = PATHS.checkpoint(phase.phase_id)
        dependencies = [PATHS.checkpoint(value) for value in phase.depends_on]
        dependencies_valid = all(states.get(value) == "SUCCEEDED" for value in phase.depends_on)
        stale = output.exists() and (
            not dependencies_valid
            or any(value.exists() and value.stat().st_mtime > output.stat().st_mtime for value in dependencies)
        )
        if stale:
            states[phase.phase_id] = "STALE"
        elif output.exists():
            states[phase.phase_id] = "SUCCEEDED"
        elif dependencies_valid:
            states[phase.phase_id] = "READY"
        else:
            states[phase.phase_id] = "BLOCKED"
    return states


def render() -> None:
    os.system("clear")
    states = phase_states()
    print(f"{BOLD}Snakemake Store Release Pipeline — THROWAWAY PROTOTYPE{RESET}")
    print(f"{DIM}One build.yaml + analyses.tsv + raw directory drive the DAG.{RESET}\n")
    print(f"{BOLD}Fixed input{RESET}")
    print(f"  Store Release   {PLAN.family_id}/{PLAN.observed_release_id}")
    print(f"  build.yaml      {CONFIG.relative_to(REPO_ROOT)}")
    print(f"  analyses.tsv    {PLAN.analyses_path.relative_to(REPO_ROOT)}")
    print(f"  raw directory   {PLAN.source_root.relative_to(REPO_ROOT)}")
    print(f"  reader          {PLAN.reader_capability}")
    print(f"  optional work   rho={PLAN.rho}  completion={PLAN.completion}\n")
    print(f"{BOLD}Phase graph{RESET}")
    for phase in phase_graph(PLAN):
        flags = []
        if phase.expensive:
            flags.append("expensive")
        if phase.mutation_mode == "in-place-update":
            flags.append("validated marker")
        suffix = f" {DIM}({', '.join(flags)}){RESET}" if flags else ""
        print(f"  {states[phase.phase_id]:10} {phase.release_id:22} {phase.phase_id}{suffix}")
    print(f"\n{BOLD}Last action{RESET}\n  {LAST_ACTION}")
    print(f"\n{BOLD}Actions{RESET}")
    print("  [f] run/resume full release   [i] interrupt observed build")
    print("  [d] Snakemake dry-run         [r] reset prototype   [q] quit")


def snakemake(*, fail_phase: str | None = None, dry_run: bool = False) -> str:
    command = [
        "snakemake",
        "--snakefile", str(HERE / "Snakefile"),
        "--configfile", str(CONFIG),
        "--cores", "1",
        "--rerun-incomplete",
    ]
    if dry_run:
        command.append("--dry-run")
    command.append("full_release")
    environment = os.environ.copy()
    if fail_phase:
        environment["PROTOTYPE_FAIL_PHASE"] = fail_phase
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
    )
    combined = result.stdout + result.stderr
    interruption = "Intentional prototype interruption"
    if interruption in combined:
        return f"exit={result.returncode}: {interruption}; rerun resumes at the build"
    transcript = combined.strip().splitlines()
    summary = " | ".join(transcript[-4:]) if transcript else "no output"
    return f"exit={result.returncode}: {summary}"


def main() -> None:
    global LAST_ACTION
    while True:
        render()
        action = input("\n> ").strip().lower()
        if action == "q":
            return
        if action == "f":
            LAST_ACTION = snakemake()
        elif action == "i":
            LAST_ACTION = snakemake(fail_phase="build_observed_store")
        elif action == "d":
            LAST_ACTION = snakemake(dry_run=True)
        elif action == "r":
            if PATHS.state_root.exists():
                shutil.rmtree(PATHS.state_root)
            LAST_ACTION = "prototype state reset"
        else:
            LAST_ACTION = f"unknown action: {action!r}"


if __name__ == "__main__":
    main()
