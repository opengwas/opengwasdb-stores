#!/usr/bin/env python3
"""Phase runner for the production Store Release workflow (issue #98).

Every Snakefile rule shells out to exactly one phase here:

    python3 workflow/phase.py --phase <phase_id> --config <build.yaml> --repo-root <repo>

`workflow/Snakefile` owns dependencies and resumption; this module owns what a
phase *means*. It is the registry-side wrapper around the OpenGWASDB CLI: it
reads the release's `build.yaml` through `resources/lib/release_plan.py`, calls
the plan's `build.command` and the other `opengwasdb` subcommands with arguments
taken from the plan, checks each phase's output by reading it back, and only
then writes the phase's completion record.

Completion records, not Store mtimes
------------------------------------
A built Store is a mutated directory -- rho writes into it, overview
regeneration rewrites it, EAF repair touches it -- so its modification time is
evidence of nothing. Each expensive or in-place phase writes
`work/completions/<phase>.json` only after its output passes the read-back
checks below, and the record is what Snakemake tracks. A partial Store therefore
has no record and cannot masquerade as a completed phase.

A record binds the phase name and Store Release identity, the sha256 of
`build.yaml`, `analyses.tsv`, every selected source file and every declared
Reference Resource descriptor, the `opengwasdb` revision and the effective
arguments, the output locations, the completion time, and the validation result.
Changing a bound input invalidates that phase and its dependents on the next run
-- Snakemake sees the newer input, and the rewritten record binds the new hashes.

Deferred branches
-----------------
The rho and Reference-Completion branches are issue #100 and are refused here
rather than silently skipped. The resolve phase is a *passthrough*: it emits
`work/analyses.resolved.tsv` and the shared builder manifest (issue #96) but
computes no ancestry or phenotype SD -- that is issue #99.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

_WORKFLOW_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WORKFLOW_DIR.parent
for _path in (str(_WORKFLOW_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from model import Workflow, WorkflowError, load_workflow  # noqa: E402
from resources.lib.release_manifest import buildable_rows, write_builder_manifest  # noqa: E402
from resources.lib.release_plan import check_release  # noqa: E402
from resources.lib.release_yaml import merge_validation_yaml, read_tsv  # noqa: E402

#: Builder-manifest columns `opengwasdb`'s Dense VCF builder requires. The
#: manifest itself is produced by the shared module (issue #96); this is the
#: read-back that it carries what the CLI documents it needs.
REQUIRED_BUILDER_COLUMNS: tuple[str, ...] = (
    "trait_id",
    "file_path",
    "trait_name",
    "n",
    "stored_effect_scale",
    "original_sd_method",
)


class PhaseError(Exception):
    """A phase whose output did not pass its read-back checks."""


# ---------------------------------------------------------------------------
# Small filesystem/CLI helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(bound: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(bound.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_tsv(path: Path, fieldnames: Sequence[str], rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
    os.replace(temporary, path)


def read_tsv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader.fieldnames or []), list(reader)


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def opengwasdb_executable() -> str:
    found = shutil.which("opengwasdb")
    if found is None:
        raise PhaseError("opengwasdb is not on PATH; run inside the `workflow` pixi environment")
    return found


def run_cli(command: Sequence[str]) -> subprocess.CompletedProcess:
    """Run one `opengwasdb` invocation, echoing its output into the run log."""
    result = subprocess.run(
        list(command),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.stdout:
        sys.stderr.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result


def opengwasdb_identity() -> dict[str, str]:
    """The `opengwasdb` revision and version this phase ran against (ADR 0022 pin)."""
    identity = {"name": "opengwasdb", "version": "", "revision": ""}
    try:
        identity["version"] = importlib.metadata.version("opengwasdb")
        distribution = importlib.metadata.distribution("opengwasdb")
        for entry in distribution.files or []:
            if str(entry).endswith("direct_url.json"):
                payload = json.loads(Path(distribution.locate_file(entry)).read_text(encoding="utf-8"))
                vcs = payload.get("vcs_info") or {}
                identity["revision"] = str(vcs.get("commit_id") or payload.get("requested_revision") or "")
                break
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        pass
    return identity


def cli_flags(arguments: dict) -> list[str]:
    """`build.arguments` as CLI flags, passed through unchanged (ADR 0022).

    Keys are CLI flag names (`store-id`, `n-workers`), not a semantic schema: a
    newly required builder flag is absorbed by the passthrough. `true` emits the
    flag alone, `false` omits it, and a list repeats the flag once per item.
    """
    flags: list[str] = []
    for name, value in arguments.items():
        flag = f"--{name}"
        if isinstance(value, bool):
            if value:
                flags.append(flag)
        elif value is None:
            continue
        elif isinstance(value, (list, tuple)):
            for item in value:
                flags.extend([flag, str(item)])
        else:
            flags.extend([flag, str(value)])
    return flags


# ---------------------------------------------------------------------------
# Completion records
# ---------------------------------------------------------------------------


def bound_inputs(workflow: Workflow) -> tuple[Path, ...]:
    """The fixed inputs every completion record binds (issue #98 contract)."""
    plan = workflow.plan
    assert plan.analyses_path is not None
    return (
        workflow.paths.config_path,
        plan.analyses_path,
        *workflow.source_paths,
        *workflow.reference_descriptors,
    )


def write_completion(
    workflow: Workflow,
    phase: str,
    *,
    outputs: Sequence[Path],
    validation: dict,
    command: Sequence[str] = (),
    extra_inputs: Sequence[Path] = (),
) -> Path:
    record_path = workflow.paths.completion(phase)
    inputs = sorted({Path(path) for path in (*bound_inputs(workflow), *extra_inputs)}, key=str)
    bound = {str(path): sha256_file(path) for path in inputs}
    record = {
        "phase": phase,
        "store_family_id": workflow.plan.store_family_id,
        "family_release_id": workflow.plan.family_release_id,
        "store_layout": workflow.plan.store_layout,
        "release_dir": str(workflow.paths.release_dir),
        "store_uri": str(workflow.paths.store_dir),
        "completed_at": datetime.now(UTC).isoformat(),
        "opengwasdb": opengwasdb_identity(),
        "command": list(command),
        "inputs": bound,
        "input_fingerprint": _fingerprint(bound),
        "outputs": [str(Path(path)) for path in outputs],
        "validation": validation,
    }
    write_json(record_path, record)
    return record_path


# ---------------------------------------------------------------------------
# Phase 1 -- validate_fixed_inputs
# ---------------------------------------------------------------------------


def phase_validate_fixed_inputs(workflow: Workflow) -> None:
    """Check the three fixed inputs and every declared resource, before anything runs.

    The release-plan loader owns build.yaml/source/checksum/reference validation;
    this phase runs it, records the outcome, and binds the checked inputs into
    its completion record.
    """
    result = check_release(workflow.paths.release_dir, verify_checksums=True)
    if result.errors:
        raise PhaseError("fixed-input validation failed:\n" + "\n".join(result.errors))

    analyses = buildable_rows(read_tsv(workflow.plan.analyses_path))
    report = workflow.paths.report("input-validation.json")
    write_json(report, {
        "status": "passed",
        "store_family_id": workflow.plan.store_family_id,
        "family_release_id": workflow.plan.family_release_id,
        "schema": workflow.plan.schema,
        "store_layout": workflow.plan.store_layout,
        "analysis_count": len(analyses),
        "source_file_count": len(workflow.source_paths),
        "reference_descriptor_count": len(workflow.reference_descriptors),
        "reader_capability": str(
            (((workflow.config.get("source") or {}).get("reader") or {}).get("capability") or "")
        ),
        "build_command": workflow.plan.build_command,
        "checksums_verified": True,
        "warnings": list(result.warnings),
    })
    write_completion(
        workflow,
        "validate_fixed_inputs",
        outputs=[report],
        validation={
            "check": "fixed_inputs",
            "status": "passed",
            "analysis_count": len(analyses),
            "warnings": list(result.warnings),
        },
    )


# ---------------------------------------------------------------------------
# Phase 2 -- resolve_analysis_metadata (passthrough; real resolution is #99)
# ---------------------------------------------------------------------------


def phase_resolve_analysis_metadata(workflow: Workflow) -> None:
    """Emit the immutable working input and the shared builder manifest.

    Passthrough by design (issue #98): no ancestry assignment and no phenotype
    SD are computed here -- issue #99 replaces this rule. What it does do is
    freeze the selection: `source_file` is resolved to an absolute path so the
    builder manifest, and therefore the build, does not depend on the working
    directory.
    """
    plan = workflow.plan
    assert plan.analyses_path is not None and plan.source_root is not None
    fieldnames, rows = read_tsv_rows(plan.analyses_path)
    buildable = buildable_rows(rows)

    resolved: list[dict[str, str]] = []
    report_rows: list[dict[str, str]] = []
    for row in buildable:
        value = (row.get("source_file") or row.get("file_name") or "").strip()
        source = Path(value)
        absolute = source if source.is_absolute() else plan.source_root / source
        resolved.append({**row, "source_file": str(absolute)})
        report_rows.append({
            "analysis_id": row.get("analysis_id", ""),
            "source_file": str(absolute),
            "ancestry_status": "passthrough",
            "effect_scale_status": "passthrough",
            "resolution": "issue #98 passthrough; real resolution is issue #99",
        })

    write_tsv(workflow.paths.resolved_analyses, fieldnames, resolved)
    manifest = write_builder_manifest(resolved, workflow.paths.builder_manifest, layout=workflow.layout)
    report = workflow.paths.report("metadata-resolution.tsv")
    write_tsv(
        report,
        ("analysis_id", "source_file", "ancestry_status", "effect_scale_status", "resolution"),
        report_rows,
    )

    failures: list[str] = []
    if len(manifest.rows) != len(buildable):
        failures.append(f"builder manifest has {len(manifest.rows)} rows for {len(buildable)} buildable Analyses")
    missing = [column for column in REQUIRED_BUILDER_COLUMNS if column not in manifest.fieldnames]
    if missing:
        failures.append("builder manifest is missing column(s): " + ", ".join(missing))
    for row in manifest.rows:
        if not Path(row.get("file_path", "")).is_file():
            failures.append(f"builder manifest row {row.get('trait_id')!r} points at a missing source file")
    if failures:
        raise PhaseError("metadata resolution read-back failed:\n" + "\n".join(failures))

    write_completion(
        workflow,
        "resolve_analysis_metadata",
        outputs=[workflow.paths.resolved_analyses, workflow.paths.builder_manifest, report],
        validation={
            "check": "resolve_metadata",
            "status": "passed",
            "resolution": "passthrough",
            "analysis_count": len(manifest.rows),
            "manifest_layout": workflow.layout,
        },
        extra_inputs=[workflow.paths.completion("validate_fixed_inputs")],
    )


# ---------------------------------------------------------------------------
# Phase 3 -- build_observed_store
# ---------------------------------------------------------------------------


def expected_analysis_ids(workflow: Workflow) -> list[str]:
    return [row["analysis_id"] for row in buildable_rows(read_tsv(workflow.plan.analyses_path))]


def store_analysis_ids(store_dir: Path) -> list[str]:
    return [row.get("analysis_id", "") for row in read_tsv(store_dir / "analyses.tsv")]


def store_info(store_dir: Path) -> dict[str, str]:
    """Read the built envelope back through `opengwasdb info`, not by hand."""
    result = run_cli([opengwasdb_executable(), "info", str(store_dir)])
    if result.returncode != 0:
        raise PhaseError(f"opengwasdb info {store_dir} exited {result.returncode}")
    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(": ")
        if separator:
            info[key.strip()] = value.strip()
    return info


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _last_json_object(output: str) -> dict:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def replace_store(partial: Path, store_dir: Path) -> None:
    """Move a fully read-back Store into place, replacing any earlier one.

    A build phase never writes into the live Store: an interrupted build leaves
    the previous Store (or none) untouched and only a `.partial` sibling behind,
    so the Store path itself never holds something a completion record does not
    vouch for.
    """
    remove_path(store_dir)
    os.replace(partial, store_dir)


def phase_build_observed_store(workflow: Workflow) -> None:
    """Run the plan's `build.command` and read the built envelope back."""
    plan = workflow.plan
    partial = workflow.paths.store_partial
    remove_path(partial)
    partial.parent.mkdir(parents=True, exist_ok=True)

    command = [
        opengwasdb_executable(),
        str(plan.build_command),
        *cli_flags(plan.build_arguments),
        str(workflow.paths.builder_manifest),
        str(partial),
    ]
    started = time.monotonic()
    result = run_cli(command)
    wall_seconds = time.monotonic() - started
    if result.returncode != 0:
        raise PhaseError(f"{' '.join(command)} exited {result.returncode}")

    summary = _last_json_object(result.stdout)
    info = store_info(partial)
    expected = expected_analysis_ids(workflow)
    actual = store_analysis_ids(partial)

    failures: list[str] = []
    if not summary:
        failures.append("the build printed no JSON summary")
    if info.get("store_id") != plan.store_family_id:
        failures.append(f"built store_id {info.get('store_id')!r} != {plan.store_family_id!r}")
    if info.get("release_id") != plan.family_release_id:
        failures.append(f"built release_id {info.get('release_id')!r} != {plan.family_release_id!r}")
    if info.get("primary_layout") != workflow.layout:
        failures.append(f"built primary_layout {info.get('primary_layout')!r} != {workflow.layout!r}")
    if not info.get("format_version"):
        failures.append("built manifest records no format_version")
    if not (partial / "data.zarr").is_dir():
        failures.append("built store has no data.zarr")
    if not (partial / "overview.html").is_file():
        failures.append("build produced no initial overview.html")
    if sorted(actual) != sorted(expected):
        failures.append(f"built Store carries Analyses {sorted(actual)}, expected {sorted(expected)}")
    if failures:
        raise PhaseError("build read-back failed:\n" + "\n".join(failures))

    replace_store(partial, workflow.paths.store_dir)

    report = workflow.paths.report("build-report.tsv")
    write_tsv(
        report,
        (
            "store_uri",
            "store_id",
            "release_id",
            "n_analyses",
            "n_variants",
            "store_bytes",
            "build_wall_seconds",
            "command",
            "opengwasdb_revision",
            "status",
        ),
        [{
            "store_uri": str(workflow.paths.store_dir),
            "store_id": plan.store_family_id,
            "release_id": plan.family_release_id,
            "n_analyses": summary.get("n_analyses", len(actual)),
            "n_variants": summary.get("n_variants", ""),
            "store_bytes": directory_bytes(workflow.paths.store_dir),
            "build_wall_seconds": f"{wall_seconds:.3f}",
            "command": " ".join(command),
            "opengwasdb_revision": opengwasdb_identity()["revision"],
            "status": "passed",
        }],
    )
    write_completion(
        workflow,
        "build_observed_store",
        outputs=[workflow.paths.store_dir / "manifest.json", report],
        validation={
            "check": "build",
            "status": "passed",
            "files_status": "passed",
            "store_uri": str(workflow.paths.store_dir),
            "n_analyses": summary.get("n_analyses", len(actual)),
            "n_variants": summary.get("n_variants", ""),
            "format_version": info.get("format_version", ""),
            "build_wall_seconds": f"{wall_seconds:.3f}",
        },
        command=command,
        extra_inputs=[workflow.paths.completion("resolve_analysis_metadata")],
    )


# ---------------------------------------------------------------------------
# Phase 4 -- regenerate_observed_overview
# ---------------------------------------------------------------------------


def phase_regenerate_observed_overview(workflow: Workflow) -> None:
    """Regenerate `overview.html` from persisted Store data and read it back.

    The build already writes an initial overview; this phase is what makes the
    final page a *regenerated* one, so a later in-place mutation (rho, #100)
    cannot leave the page stale.
    """
    store_dir = workflow.paths.store_dir
    command = [opengwasdb_executable(), "regenerate-overview", str(store_dir)]
    result = run_cli(command)
    if result.returncode != 0:
        raise PhaseError(f"{' '.join(command)} exited {result.returncode}")

    overview = store_dir / "overview.html"
    failures: list[str] = []
    if not overview.is_file():
        failures.append("regenerate-overview left no overview.html")
    else:
        html = overview.read_text(encoding="utf-8")
        if not html.strip():
            failures.append("regenerated overview.html is empty")
        missing = [analysis_id for analysis_id in store_analysis_ids(store_dir) if analysis_id not in html]
        if missing:
            failures.append("regenerated overview.html omits Analyses: " + ", ".join(missing))
    if failures:
        raise PhaseError("overview read-back failed:\n" + "\n".join(failures))

    write_completion(
        workflow,
        "regenerate_observed_overview",
        outputs=[overview],
        validation={
            "check": "overview",
            "status": "passed",
            "overview_sha256": sha256_file(overview),
            "overview_bytes": overview.stat().st_size,
        },
        command=command,
        extra_inputs=[workflow.paths.completion("build_observed_store")],
    )


# ---------------------------------------------------------------------------
# Phase 5 -- validate_observed_release
# ---------------------------------------------------------------------------


def _prefixed_lines(output: str, prefix: str) -> list[str]:
    return [line.split(prefix, 1)[1].strip() for line in output.splitlines() if prefix in line]


def phase_validate_observed_release(workflow: Workflow) -> None:
    """Validate the Store with `opengwasdb validate` and record the result.

    The CLI prints a result and exits; it does not write this registry's
    `validation.yaml`. This phase is the registry-side wrapper that merges the
    CLI result in, the way the existing assessment scripts do. `validation.yaml`
    is this phase's output alone -- one writer, so a merge can never race
    Snakemake deleting a stale output of a different rule.
    """
    store_dir = workflow.paths.store_dir
    command = [opengwasdb_executable(), "validate", str(store_dir)]
    result = run_cli(command)
    warnings = _prefixed_lines(result.stderr, "warning: ")
    errors = _prefixed_lines(result.stderr, "error: ")
    if result.returncode != 0 and not errors:
        errors.append(f"{' '.join(command)} exited {result.returncode}")

    status = "passed" if result.returncode == 0 and not errors else "failed"
    build_record = json.loads(workflow.paths.completion("build_observed_store").read_text(encoding="utf-8"))
    files_status = str((build_record.get("validation") or {}).get("files_status", "not_run"))
    validation_yaml = workflow.paths.validation_yaml
    merge_validation_yaml(
        validation_yaml,
        validator_name="workflow/phase.py:validate_observed_release",
        updated_checks={"schema": status, "files": files_status, "store": status},
        updated_reports={
            "input_validation": "sidecars/input-validation.json",
            "metadata_resolution": "sidecars/metadata-resolution.tsv",
            "build_report": "sidecars/build-report.tsv",
        },
        new_warnings=warnings,
    )
    if status != "passed":
        raise PhaseError("Store validation failed:\n" + "\n".join(errors))
    write_completion(
        workflow,
        "validate_observed_release",
        outputs=[validation_yaml],
        validation={
            "check": "validate",
            "status": status,
            "store_uri": str(store_dir),
            "warnings": warnings,
            "validation_yaml": str(validation_yaml),
        },
        command=command,
        extra_inputs=[
            workflow.paths.completion("regenerate_observed_overview"),
            store_dir / "overview.html",
        ],
    )


PHASES = {
    "validate_fixed_inputs": phase_validate_fixed_inputs,
    "resolve_analysis_metadata": phase_resolve_analysis_metadata,
    "build_observed_store": phase_build_observed_store,
    "regenerate_observed_overview": phase_regenerate_observed_overview,
    "validate_observed_release": phase_validate_observed_release,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Store Release workflow phase.")
    parser.add_argument("--phase", required=True, choices=sorted(PHASES))
    parser.add_argument("--config", required=True, type=Path, help="the release's build.yaml")
    parser.add_argument("--repo-root", required=True, type=Path, help="repository root the release belongs to")
    args = parser.parse_args(argv)

    try:
        workflow = load_workflow(args.config, args.repo_root)
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        PHASES[args.phase](workflow)
    except PhaseError as exc:
        print(f"error: phase {args.phase}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
