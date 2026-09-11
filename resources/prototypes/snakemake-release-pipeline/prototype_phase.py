#!/usr/bin/env python3
"""Cheap stand-ins for real Store Release phases. PROTOTYPE — do not ship."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from pipeline_model import load_prototype, source_files


def digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_dir():
        for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
            digest.update(str(child.relative_to(path)).encode())
            digest.update(child.read_bytes())
    else:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def combined_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=str):
        digest.update(str(path).encode())
        digest.update(digest_path(path).encode())
    return digest.hexdigest()


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value)
    os.replace(temporary, path)


def write_json(path: Path, value: dict) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def checkpoint(
    paths,
    phase_id: str,
    inputs: list[Path],
    artifacts: list[Path],
    *,
    operation: str,
    mutation_mode: str = "new-output",
) -> None:
    write_json(
        paths.checkpoint(phase_id),
        {
            "phase_id": phase_id,
            "status": "succeeded",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "input_fingerprint": combined_digest(inputs),
            "inputs": {str(path): digest_path(path) for path in inputs},
            "artifacts": [str(path) for path in artifacts],
            "operation": operation,
            "mutation_mode": mutation_mode,
        },
    )


def store_files(store: Path, release_id: str, operation: str) -> None:
    write_json(store / "manifest.json", {"release_id": release_id, "operation": operation})
    write_text(store / "analyses.tsv", "analysis_id\ttrait_label\nEXAMPLE-001\tExample trait\n")
    write_text(store / "variants.tsv.bgz", "prototype compressed variant table\n")
    write_text(store / "data.zarr" / "z" / ".zarray", "{}\n")
    write_text(store / "top-hits" / "metadata.json", '{"built_by": "core build"}\n')
    write_text(store / "overview.html", "<html><body>Initial overview</body></html>\n")


def run_phase(phase_id: str, plan, paths, config) -> None:
    observed_id = plan.observed_release_id
    observed_store = paths.store_dir(plan.family_id, observed_id)
    raw_files = list(source_files(plan))

    if phase_id == "validate_fixed_inputs":
        with plan.analyses_path.open(newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            required = {"analysis_id", "file_name"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise SystemExit("analyses.tsv is missing analysis_id or file_name")
            rows = list(reader)
        if not rows or any(not path.is_file() for path in raw_files):
            raise SystemExit("analyses.tsv is empty or references a missing source file")
        report = paths.report(observed_id, "input-validation.json")
        write_json(
            report,
            {
                "status": "passed",
                "analysis_count": len(rows),
                "reader_capability": plan.reader_capability,
                "source_root": str(plan.source_root),
            },
        )
        inputs = [paths.config_path, plan.analyses_path, *raw_files]
        checkpoint(paths, phase_id, inputs, [report], operation="validate fixed release inputs")
        return

    if phase_id == "resolve_analysis_metadata":
        source_lines = plan.analyses_path.read_text().splitlines()
        resolved = paths.resolved_analyses()
        write_text(resolved, source_lines[0] + "\tresolved_ancestry\n" + "\n".join(
            line + "\tEuropean" for line in source_lines[1:]
        ) + "\n")
        report = paths.report(observed_id, "metadata-resolution.tsv")
        write_text(report, "analysis_id\tancestry_status\teffect_scale_status\nEXAMPLE-001\tresolved\tresolved\n")
        inputs = [paths.checkpoint("validate_fixed_inputs"), plan.analyses_path]
        checkpoint(paths, phase_id, inputs, [resolved, report], operation="resolve ancestry and effect scale")
        return

    if phase_id == "build_observed_store":
        observed_store.mkdir(parents=True, exist_ok=True)
        write_text(observed_store / "PARTIAL", "prototype partial Store\n")
        fail_once = paths.state_root / "fail-once" / phase_id
        if os.environ.get("PROTOTYPE_FAIL_PHASE") == phase_id and not fail_once.exists():
            write_text(fail_once, "failed once\n")
            raise SystemExit(
                "Intentional prototype interruption; partial Store has no completion record"
            )
        store_files(observed_store, observed_id, plan.build_operation)
        (observed_store / "PARTIAL").unlink(missing_ok=True)
        report = paths.report(observed_id, "build-report.tsv")
        write_text(report, "operation\tstatus\ttop_hits\n" + f"{plan.build_operation}\tpassed\tbuilt\n")
        inputs = [paths.checkpoint("resolve_analysis_metadata"), paths.resolved_analyses(), paths.config_path, *raw_files]
        checkpoint(paths, phase_id, inputs, [observed_store, report], operation=plan.build_operation)
        return

    if phase_id in {"build_observed_rho", "build_completed_rho"}:
        completed = phase_id.startswith("build_completed")
        release_id = plan.completed_release_id if completed else observed_id
        assert release_id
        store = paths.store_dir(plan.family_id, release_id)
        prior = "complete_store" if completed else "build_observed_store"
        rho = store / "data.zarr" / "rho" / "metadata.json"
        report = paths.report(release_id, "rho-report.json")
        write_json(rho, {"window_bp": config["rho"]["window_bp"], "status": "built"})
        write_json(report, {"status": "passed", "release_id": release_id})
        checkpoint(
            paths,
            phase_id,
            [paths.checkpoint(prior), store / "manifest.json"],
            [rho, report],
            operation="opengwasdb rho build",
            mutation_mode="in-place-update",
        )
        return

    if phase_id in {"regenerate_observed_overview", "regenerate_completed_overview"}:
        completed = phase_id.startswith("regenerate_completed")
        release_id = plan.completed_release_id if completed else observed_id
        assert release_id
        store = paths.store_dir(plan.family_id, release_id)
        prior = (
            "build_completed_rho" if completed and plan.rho else
            "complete_store" if completed else
            "build_observed_rho" if plan.rho else
            "build_observed_store"
        )
        overview = store / "overview.html"
        write_text(
            overview,
            "<html><body>Analyses | Ancestry | Rho | Guide</body></html>\n",
        )
        checkpoint(
            paths,
            phase_id,
            [paths.checkpoint(prior), store / "manifest.json"],
            [overview],
            operation="opengwasdb regenerate-overview",
            mutation_mode="in-place-update",
        )
        return

    if phase_id in {"validate_observed_release", "validate_completed_release"}:
        completed = phase_id.startswith("validate_completed")
        release_id = plan.completed_release_id if completed else observed_id
        assert release_id
        store = paths.store_dir(plan.family_id, release_id)
        prior = "regenerate_completed_overview" if completed else "regenerate_observed_overview"
        required = [store / "manifest.json", store / "analyses.tsv", store / "overview.html"]
        if any(not path.exists() for path in required) or (store / "PARTIAL").exists():
            raise SystemExit(f"Store validation failed for {store}")
        validation = paths.report(release_id, "validation.yaml")
        write_text(validation, yaml.safe_dump({"status": "passed", "release_id": release_id}))
        checkpoint(paths, phase_id, [paths.checkpoint(prior), *required], [validation], operation="validate final Store")
        return

    if phase_id == "register_completed_release":
        assert plan.completed_release_id and plan.completion_operation
        release = paths.release_dir(plan.family_id, plan.completed_release_id) / "release.yaml"
        write_text(
            release,
            yaml.safe_dump(
                {
                    "store_family_id": plan.family_id,
                    "family_release_id": plan.completed_release_id,
                    "lineage": {"derived_from": observed_id},
                    "completion_state": "reference-completed",
                },
                sort_keys=False,
            ),
        )
        checkpoint(
            paths,
            phase_id,
            [paths.checkpoint("validate_observed_release"), paths.config_path],
            [release],
            operation="register reference-completed descendant",
        )
        return

    if phase_id == "complete_store":
        assert plan.completed_release_id and plan.completion_operation
        child = paths.store_dir(plan.family_id, plan.completed_release_id)
        if child.exists():
            shutil.rmtree(child)
        shutil.copytree(observed_store, child)
        store_files(child, plan.completed_release_id, plan.completion_operation)
        report = paths.report(plan.completed_release_id, "completion-report.tsv")
        write_text(report, "operation\tstatus\n" + f"{plan.completion_operation}\tpassed\n")
        inputs = [paths.checkpoint("register_completed_release"), observed_store]
        checkpoint(paths, phase_id, inputs, [child, report], operation=plan.completion_operation)
        return

    raise SystemExit(f"Unknown prototype phase: {phase_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase_id")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    plan, paths, config = load_prototype(args.repo_root.resolve(), args.config.resolve())
    run_phase(args.phase_id, plan, paths, config)


if __name__ == "__main__":
    main()
