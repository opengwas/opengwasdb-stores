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

Optional branches
----------------
The rho and Reference-Completion branches (issue #100) are wired here, not
refused. Rho is Dense-only and is refused by `resources/lib/release_plan.py`
before any Store work; this module runs it as an in-place mutation followed
strictly by overview regeneration. Reference Completion registers a
lineage-linked child Store Release (ADR 0007) and builds a separate Store, then
runs the child's own rho, overview, and validation phases. The resolve phase
owns real metadata resolution (issue #99): it computes Assigned Ancestry and
proportions and effect-scale / phenotype SD into `work/analyses.resolved.tsv`,
never mutating the committed `analyses.tsv`.
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

from model import ReleaseSite, Workflow, WorkflowError, load_workflow  # noqa: E402
from resources.lib.metadata_resolution import ResolutionError, resolve_analyses  # noqa: E402
from resources.lib.release_manifest import buildable_rows, write_builder_manifest  # noqa: E402
from resources.lib.release_plan import check_release  # noqa: E402
from resources.lib.release_yaml import (  # noqa: E402
    merge_release_yaml,
    merge_validation_yaml,
    read_tsv,
    split_top_level_blocks,
    top_level_scalars,
    write_release_status,
)

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
    site: ReleaseSite | None = None,
) -> Path:
    """Write a phase's completion record for the observed release or its child.

    `site` is `workflow.observed_site` unless a Reference-Completion phase passes
    the child's site, so the record always names the Release it belongs to.
    """
    target = site if site is not None else workflow.observed_site
    record_path = target.completion(phase)
    inputs = sorted({Path(path) for path in (*bound_inputs(workflow), *extra_inputs)}, key=str)
    bound = {str(path): sha256_file(path) for path in inputs}
    record = {
        "phase": phase,
        "store_family_id": target.store_family_id,
        "family_release_id": target.release_id,
        "store_layout": target.layout,
        "release_dir": str(target.release_dir),
        "store_uri": str(target.store_dir),
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
# Phase 2 -- resolve_analysis_metadata (real ancestry + effect-scale; issue #99)
# ---------------------------------------------------------------------------


def phase_resolve_analysis_metadata(workflow: Workflow) -> None:
    """Compute real ancestry and effect-scale metadata into the working input.

    Reads the committed ``analyses.tsv`` and never writes it (issue #99, AC1):
    the resolved table is a *new* file, so a rebuild is reproducible from the
    registry alone and no phase mutates the fixed input. Computes Assigned
    Ancestry and proportions (AF mixture fit against the declared reference),
    and effect-scale / phenotype SD (source-AF estimate or declared-standardised
    verification), writes ``work/analyses.resolved.tsv`` and the resolution
    report, and persists the release-level checks in its completion record so the
    validate phase can merge them into ``validation.yaml`` without a second
    writer of that file.

    A failed effect-scale check is evidence, not a workflow failure by default
    (issue #99 decision 2): the release lands as ``built``. A family that wants
    it blocking sets ``effect_scale_validation.block_on_failure: yes``.
    """
    plan = workflow.plan
    assert plan.analyses_path is not None and plan.source_root is not None
    fieldnames, rows = read_tsv_rows(plan.analyses_path)
    buildable = buildable_rows(rows)

    try:
        resolution = resolve_analyses(
            buildable,
            fieldnames=fieldnames,
            source_root=plan.source_root,
            config=workflow.config,
            repo_root=workflow.paths.repo_root,
        )
    except ResolutionError as exc:
        raise PhaseError(f"metadata resolution failed: {exc}") from exc

    write_tsv(workflow.paths.resolved_analyses, resolution.fieldnames, resolution.rows)
    report = workflow.paths.report("metadata-resolution.tsv")
    write_tsv(report, resolution.report_columns, resolution.report_rows)
    manifest = write_builder_manifest(
        resolution.rows, workflow.paths.builder_manifest, layout=workflow.layout
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

    validation = {
        "check": "resolve_metadata",
        "status": "passed",
        "resolution": "af_ancestry_and_effect_scale",
        "analysis_count": len(manifest.rows),
        "manifest_layout": workflow.layout,
        "derived_analysis_count": len(resolution.derived_analysis_ids),
        "derived_analyses": list(resolution.derived_analysis_ids),
        "warnings": list(resolution.warnings),
        **resolution.checks,
    }
    if resolution.blocked:
        # Write the record first so the failure is auditable, then block.
        write_completion(
            workflow,
            "resolve_analysis_metadata",
            outputs=[workflow.paths.resolved_analyses, workflow.paths.builder_manifest, report],
            validation={**validation, "status": "blocked"},
            extra_inputs=[workflow.paths.completion("validate_fixed_inputs")],
        )
        raise PhaseError(
            "effect-scale validation failed and effect_scale_validation.block_on_failure is set "
            "for this release; refusing to build\n"
            + "\n".join(w for w in resolution.warnings if "effect-scale" in w)
        )
    write_completion(
        workflow,
        "resolve_analysis_metadata",
        outputs=[workflow.paths.resolved_analyses, workflow.paths.builder_manifest, report],
        validation=validation,
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
# Phase 4 -- build_observed_rho / build_completed_rho (in-place, Dense-only)
# ---------------------------------------------------------------------------


def rho_group(store_dir: Path) -> dict:
    """Read `data.zarr/rho` back through zarr, the group `build-dense-rho` adds.

    Rho is an opt-in add-in-place group (ADR 0025), so "rho ran" means the group
    exists with its provenance attributes -- not that a file was written. The
    import is local because the group only exists in the `workflow` environment.
    """
    import zarr  # noqa: PLC0415 -- only the workflow environment carries stores

    root = zarr.open_group(str(store_dir / "data.zarr"), mode="r")
    if "rho" not in root:
        raise PhaseError(f"{store_dir} has no data.zarr/rho group after build-dense-rho")
    group = root["rho"]
    return {
        "method": str(group.attrs.get("method", "")),
        "n_analyses": int(group.attrs.get("n_analyses", 0)),
        "n_variants_used": int(group.attrs.get("n_variants_used", 0)),
        "window_bp": int(group.attrs.get("window_bp", 0)),
        "z_thresh": float(group.attrs.get("z_thresh", 0.0)),
        "min_nulls": int(group.attrs.get("min_nulls", 0)),
    }


def phase_build_rho(workflow: Workflow, site: ReleaseSite, *, phase_id: str, upstream_phase: str) -> None:
    """Build the Rho Matrix into `site`'s Store, in place, and read it back.

    `build-dense-rho` is an in-place mutation -- it adds `data.zarr/rho` to the
    Store the build already made. The overview phase depends on this phase's
    record, so `overview.html` is always regenerated after it.
    """
    store_dir = site.store_dir
    if not (store_dir / "manifest.json").is_file():
        raise PhaseError(f"rho phase ran before a Store existed at {store_dir}")
    command = [opengwasdb_executable(), "build-dense-rho", str(store_dir), *cli_flags(workflow.rho_arguments)]
    result = run_cli(command)
    if result.returncode != 0:
        raise PhaseError(f"{' '.join(command)} exited {result.returncode}")

    attrs = rho_group(store_dir)
    expected = expected_analysis_ids(workflow)
    failures: list[str] = []
    if not attrs["method"]:
        failures.append("data.zarr/rho records no rho method")
    if attrs["n_analyses"] != len(expected):
        failures.append(f"data.zarr/rho covers {attrs['n_analyses']} Analyses, expected {len(expected)}")
    if failures:
        raise PhaseError("rho read-back failed:\n" + "\n".join(failures))

    report = site.report("rho-report.json")
    write_json(report, {
        "status": "passed",
        "store_family_id": site.store_family_id,
        "family_release_id": site.release_id,
        "store_uri": str(store_dir),
        "command": command,
        **attrs,
    })
    write_completion(
        workflow,
        phase_id,
        site=site,
        outputs=[report],
        validation={"check": "rho", "status": "passed", **attrs},
        command=command,
        extra_inputs=[site.completion(upstream_phase)],
    )


def phase_build_observed_rho(workflow: Workflow) -> None:
    phase_build_rho(
        workflow,
        workflow.observed_site,
        phase_id="build_observed_rho",
        upstream_phase="build_observed_store",
    )


def phase_build_completed_rho(workflow: Workflow) -> None:
    phase_build_rho(
        workflow,
        workflow.completion_site,
        phase_id="build_completed_rho",
        upstream_phase="complete_store",
    )


# ---------------------------------------------------------------------------
# Phase 5 -- regenerate_overview (observed and child)
# ---------------------------------------------------------------------------

#: The tab `opengwasdb regenerate-overview` emits only when `data.zarr/rho`
#: exists (ADR 0025). Asserting it appears is what makes "the page reflects the
#: post-rho Store" checkable; asserting it is absent when rho is disabled is
#: what makes "rho disabled skips it" checkable.
RHO_TAB_MARKER = 'data-tab="rho"'


def phase_regenerate_overview(
    workflow: Workflow, site: ReleaseSite, *, phase_id: str, upstream_phase: str
) -> None:
    """Regenerate `overview.html` from persisted Store data and read it back.

    `regenerate-overview` rewrites the page from the Store's already-persisted
    `analyses.tsv`/`manifest.json` and a directory scan, so it must run after
    every in-place mutation; `upstream_phase` is the phase that record makes
    that ordering explicit in the DAG.
    """
    store_dir = site.store_dir
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
        if workflow.rho_enabled and RHO_TAB_MARKER not in html:
            failures.append("regenerated overview.html has no Rho tab after rho was built")
        if not workflow.rho_enabled and RHO_TAB_MARKER in html:
            failures.append("regenerated overview.html has a Rho tab though rho is disabled")
    if failures:
        raise PhaseError("overview read-back failed:\n" + "\n".join(failures))

    write_completion(
        workflow,
        phase_id,
        site=site,
        outputs=[overview],
        validation={
            "check": "overview",
            "status": "passed",
            "rho_tab": workflow.rho_enabled,
            "overview_sha256": sha256_file(overview),
            "overview_bytes": overview.stat().st_size,
        },
        command=command,
        extra_inputs=[site.completion(upstream_phase)],
    )


def phase_regenerate_observed_overview(workflow: Workflow) -> None:
    phase_regenerate_overview(
        workflow,
        workflow.observed_site,
        phase_id="regenerate_observed_overview",
        upstream_phase="build_observed_rho" if workflow.rho_enabled else "build_observed_store",
    )


def phase_regenerate_completed_overview(workflow: Workflow) -> None:
    phase_regenerate_overview(
        workflow,
        workflow.completion_site,
        phase_id="regenerate_completed_overview",
        upstream_phase="build_completed_rho" if workflow.rho_enabled else "complete_store",
    )


# ---------------------------------------------------------------------------
# Phase 6 -- validate (observed and child)
# ---------------------------------------------------------------------------


def _prefixed_lines(output: str, prefix: str) -> list[str]:
    return [line.split(prefix, 1)[1].strip() for line in output.splitlines() if prefix in line]


def phase_validate(
    workflow: Workflow,
    site: ReleaseSite,
    *,
    phase_id: str,
    build_phase: str,
    overview_phase: str,
    reports: dict[str, str],
    resolve_phase: str | None = None,
) -> None:
    """Validate `site`'s Store with `opengwasdb validate` and record the result.

    The CLI prints a result and exits; it does not write this registry's
    `validation.yaml`. This phase is the registry-side wrapper that merges the
    CLI result in, the way the existing assessment scripts do. `validation.yaml`
    is this phase's output alone -- one writer, so a merge can never race
    Snakemake deleting a stale output of a different rule.

    `resolve_phase` is named for the observed release only. When it is set, the
    resolve phase's release-level checks (`ancestry`, `effect_scale`,
    `sd_estimation`) and warnings are merged here too, and the release's
    lifecycle status is landed: a failed effect-scale check is evidence, so the
    release is `built` rather than `validated` (issue #99). A Reference
    Completion child has no resolve phase, so it only lands `schema`/`files`/
    `store` and its own `validation.yaml`.
    """
    store_dir = site.store_dir
    command = [opengwasdb_executable(), "validate", str(store_dir)]
    result = run_cli(command)
    warnings = _prefixed_lines(result.stderr, "warning: ")
    errors = _prefixed_lines(result.stderr, "error: ")
    if result.returncode != 0 and not errors:
        errors.append(f"{' '.join(command)} exited {result.returncode}")

    status = "passed" if result.returncode == 0 and not errors else "failed"
    build_record = json.loads(site.completion(build_phase).read_text(encoding="utf-8"))
    files_status = str((build_record.get("validation") or {}).get("files_status", "not_run"))
    validation_yaml = site.validation_yaml
    updated_checks = {"schema": status, "files": files_status, "store": status}
    new_warnings = list(warnings)
    outputs = [validation_yaml]
    extra_inputs = [site.completion(overview_phase), store_dir / "overview.html"]
    release_status = "validated"
    status_changed = False
    effect_scale_status = "not_run"
    if resolve_phase is not None:
        resolve_record = json.loads(site.completion(resolve_phase).read_text(encoding="utf-8"))
        resolve_validation = resolve_record.get("validation") or {}
        effect_scale_status = str(resolve_validation.get("effect_scale", "not_run"))
        updated_checks.update({
            "ancestry": str(resolve_validation.get("ancestry", "not_run")),
            "effect_scale": effect_scale_status,
            "sd_estimation": str(resolve_validation.get("sd_estimation", "not_run")),
        })
        new_warnings = [*list(resolve_validation.get("warnings", [])), *warnings]
        outputs = [validation_yaml, site.release_yaml]
        extra_inputs = [
            site.completion(resolve_phase),
            site.completion(overview_phase),
            store_dir / "overview.html",
        ]
        # Release Status (CONTEXT.md): a Store that built and validated but whose
        # effect-scale evidence failed is `built`, not `validated`; the failure is
        # retained as evidence rather than silently rescaled (issue #99).
        release_status = "built" if effect_scale_status == "failed" else "validated"
        status_changed = write_release_status(site.release_yaml, release_status)

    merge_validation_yaml(
        validation_yaml,
        validator_name=f"workflow/phase.py:{phase_id}",
        updated_checks=updated_checks,
        updated_reports=reports,
        new_warnings=new_warnings,
    )
    if status != "passed":
        raise PhaseError("Store validation failed:\n" + "\n".join(errors))

    validation: dict = {
        "check": "validate",
        "status": status,
        "store_uri": str(store_dir),
        "warnings": warnings,
        "validation_yaml": str(validation_yaml),
    }
    if resolve_phase is not None:
        validation["release_status"] = release_status
        validation["release_status_changed"] = status_changed
        validation["effect_scale_status"] = effect_scale_status
    write_completion(
        workflow,
        phase_id,
        site=site,
        outputs=outputs,
        validation=validation,
        command=command,
        extra_inputs=extra_inputs,
    )


def phase_validate_observed_release(workflow: Workflow) -> None:
    reports = {
        "input_validation": "sidecars/input-validation.json",
        "metadata_resolution": "sidecars/metadata-resolution.tsv",
        "build_report": "sidecars/build-report.tsv",
    }
    if workflow.rho_enabled:
        reports["rho_report"] = "sidecars/rho-report.json"
    phase_validate(
        workflow,
        workflow.observed_site,
        phase_id="validate_observed_release",
        build_phase="build_observed_store",
        overview_phase="regenerate_observed_overview",
        reports=reports,
        resolve_phase="resolve_analysis_metadata",
    )


def phase_validate_completed_release(workflow: Workflow) -> None:
    reports = {"completion_report": "sidecars/completion-report.tsv"}
    if workflow.rho_enabled:
        reports["rho_report"] = "sidecars/rho-report.json"
    phase_validate(
        workflow,
        workflow.completion_site,
        phase_id="validate_completed_release",
        build_phase="complete_store",
        overview_phase="regenerate_completed_overview",
        reports=reports,
    )


# ---------------------------------------------------------------------------
# Phase 7 -- register_completed_release (ADR 0007 child registration)
# ---------------------------------------------------------------------------


def phase_register_completed_release(workflow: Workflow) -> None:
    """Register the lineage-linked child Store Release in its own Release Bundle.

    Reference Completion is a *distinct* Store Release whose lineage names the
    observed parent (ADR 0007), never an in-place mutation of it. The child's
    bundle is the observed bundle's sibling, matching the layout the checked-in
    trial releases use (`families/<family>/releases/<observed[-completed]>/`).
    """
    child = workflow.child
    plan = workflow.plan
    if child is None:
        raise PhaseError("this release does not enable Reference Completion")

    # Workflow-owned blocks, refreshed on every re-registration. `description`
    # and `notes` block scalars, `source_*`, `release_kind`,
    # `association_coverage`, `accepted_at`, `build_environment`,
    # `source_defaults`, and anything a curator adds later are preserved
    # verbatim by `merge_release_yaml`, so re-registering refreshes provenance
    # without discarding the bundle's curated record (issue #101).
    owned: dict[str, list[str]] = {
        "metadata_schema_version": ["metadata_schema_version: 1"],
        "store_family_id": [f"store_family_id: {plan.store_family_id}"],
        "family_release_id": [f"family_release_id: {child.release_id}"],
        "store_layout": [f"store_layout: {child.layout}"],
        "completion_state": ["completion_state: reference-completed"],
        "lineage": ["lineage:", f"  derived_from: {plan.family_release_id}"],
        "generator": [
            "generator:",
            "  name: workflow/phase.py:register_completed_release",
            "  version: null",
            f"  command: {child.command}",
        ],
    }
    # Lifecycle state and creation time are seeded for a new child, never reset on
    # re-registration: a curator's `status` (or a later validation's) stands.
    present = (
        {key for key, _ in split_top_level_blocks(child.release_yaml.read_text(encoding="utf-8")) if key}
        if child.release_yaml.exists()
        else set()
    )
    if "status" not in present:
        owned["status"] = ["status: built"]
    if "created_at" not in present:
        owned["created_at"] = [f"created_at: '{datetime.now(UTC).isoformat()}'"]

    child.release_dir.mkdir(parents=True, exist_ok=True)
    merge_release_yaml(child.release_yaml, owned)

    registered = top_level_scalars(child.release_yaml.read_text(encoding="utf-8"))
    failures: list[str] = []
    if registered.get("family_release_id") != child.release_id:
        failures.append(
            f"registered family_release_id {registered.get('family_release_id')!r} != {child.release_id!r}"
        )
    if registered.get("derived_from") != str(plan.family_release_id):
        failures.append(
            f"registered lineage.derived_from {registered.get('derived_from')!r} != {plan.family_release_id!r}"
        )
    if registered.get("store_layout") != child.layout:
        failures.append(
            f"registered store_layout {registered.get('store_layout')!r} != {child.layout!r}"
        )
    if registered.get("completion_state") != "reference-completed":
        failures.append(
            f"registered completion_state {registered.get('completion_state')!r} != 'reference-completed'"
        )
    if failures:
        raise PhaseError("child registration read-back failed:\n" + "\n".join(failures))

    write_completion(
        workflow,
        "register_completed_release",
        site=workflow.completion_site,
        outputs=[child.release_yaml],
        validation={
            "check": "register_completion_child",
            "status": "passed",
            "child_release_id": child.release_id,
            "derived_from": plan.family_release_id,
            "release_yaml": str(child.release_yaml),
        },
        extra_inputs=[workflow.paths.completion("validate_observed_release")],
    )


# ---------------------------------------------------------------------------
# Phase 8 -- complete_store (build the child from the observed Store)
# ---------------------------------------------------------------------------

#: Completion subcommands that ship a `-resume` sibling, and the command that
#: continues an interrupted run of them from an existing checkpoint directory.
COMPLETION_RESUME_COMMANDS = {"complete-dense": "complete-dense-resume"}


def _n_workers_flags(arguments: dict) -> list[str]:
    """The `--n-workers` flag an interrupted completion is resumed with, if set."""
    value = arguments.get("n-workers")
    return [] if value is None else ["--n-workers", str(value)]


def completion_command(workflow: Workflow) -> list[str]:
    """The fresh `opengwasdb` completion invocation for the child Store.

    The observed Store is the source and a `.partial` sibling of the child Store
    is the destination, so an interrupted completion never leaves a half-written
    Store at the child's own path. `reference_completion.arguments` is an opaque
    passthrough, exactly like `build.arguments`.
    """
    child = workflow.child
    if child is None:
        raise PhaseError("this release does not enable Reference Completion")
    return [
        opengwasdb_executable(),
        child.command,
        str(workflow.paths.store_dir),
        str(child.store_partial),
        *cli_flags(child.arguments),
    ]


def resume_checkpoint(workflow: Workflow, fresh_command: Sequence[str]) -> Path | None:
    """The checkpoint directory an interrupted completion left, if resumable.

    `opengwasdb` writes a per-block checkpoint directory before it finishes, and
    its `-resume` subcommand loads every parameter from that directory's
    `build_params.json`. The phase writes the exact fresh command it issued next
    to the checkpoint, so a later invocation resumes only when that command is
    unchanged; a checkpoint from a different configuration is discarded rather
    than resumed against the wrong reference panel.
    """
    child = workflow.child
    if child is None:
        return None
    checkpoint = child.store_partial.parent / f".{child.store_partial.name}.checkpoint"
    marker = child.work_dir / "completion-command.json"
    if checkpoint.is_dir() and marker.is_file():
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except ValueError:
            recorded = None
        if recorded == {"command": list(fresh_command), "dest": str(child.store_partial)}:
            return checkpoint
    remove_path(checkpoint)
    marker.unlink(missing_ok=True)
    return None


def phase_complete_store(workflow: Workflow) -> None:
    """Build the child Store Release from the observed Store, never mutating it."""
    child = workflow.child
    plan = workflow.plan
    if child is None:
        raise PhaseError("this release does not enable Reference Completion")
    source = workflow.paths.store_dir
    if not (source / "manifest.json").is_file():
        raise PhaseError(f"the observed Store {source} does not exist; build it before completion")

    dest = child.store_partial
    remove_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    fresh_command = completion_command(workflow)
    checkpoint = resume_checkpoint(workflow, fresh_command)
    resume_command = COMPLETION_RESUME_COMMANDS.get(child.command)
    marker = child.work_dir / "completion-command.json"
    if checkpoint is not None and resume_command is not None:
        command = [opengwasdb_executable(), resume_command, str(checkpoint), *_n_workers_flags(child.arguments)]
    else:
        # A checkpoint from a command with no `-resume` sibling is not resumable:
        # start clean rather than leaving `opengwasdb` to fail on it.
        if checkpoint is not None:
            remove_path(checkpoint)
        command = fresh_command
        write_json(marker, {"command": list(fresh_command), "dest": str(dest)})

    started = time.monotonic()
    result = run_cli(command)
    wall_seconds = time.monotonic() - started
    if result.returncode != 0:
        raise PhaseError(f"{' '.join(command)} exited {result.returncode}")

    summary = _last_json_object(result.stdout)
    info = store_info(dest)
    expected = expected_analysis_ids(workflow)
    actual = store_analysis_ids(dest)

    failures: list[str] = []
    if not summary:
        failures.append("the completion printed no JSON summary")
    if info.get("store_id") != plan.store_family_id:
        failures.append(f"completed store_id {info.get('store_id')!r} != {plan.store_family_id!r}")
    if info.get("release_id") != child.release_id:
        failures.append(f"completed release_id {info.get('release_id')!r} != {child.release_id!r}")
    if info.get("primary_layout") != workflow.layout:
        failures.append(f"completed primary_layout {info.get('primary_layout')!r} != {workflow.layout!r}")
    if info.get("completion_state") != "reference_completed":
        failures.append(f"completed completion_state {info.get('completion_state')!r} != 'reference_completed'")
    if not (dest / "data.zarr").is_dir():
        failures.append("completed Store has no data.zarr")
    if sorted(actual) != sorted(expected):
        failures.append(f"completed Store carries Analyses {sorted(actual)}, expected {sorted(expected)}")
    if failures:
        raise PhaseError("completion read-back failed:\n" + "\n".join(failures))

    replace_store(dest, child.store_dir)
    marker.unlink(missing_ok=True)

    report = child.report("completion-report.tsv")
    write_tsv(
        report,
        (
            "store_uri",
            "source_store_uri",
            "store_id",
            "release_id",
            "n_analyses",
            "n_variants",
            "n_imputed",
            "completion_wall_seconds",
            "command",
            "opengwasdb_revision",
            "status",
        ),
        [{
            "store_uri": str(child.store_dir),
            "source_store_uri": str(source),
            "store_id": plan.store_family_id,
            "release_id": child.release_id,
            "n_analyses": summary.get("n_analyses", len(actual)),
            "n_variants": summary.get("n_variants", ""),
            "n_imputed": summary.get("n_imputed", ""),
            "completion_wall_seconds": f"{wall_seconds:.3f}",
            "command": " ".join(command),
            "opengwasdb_revision": opengwasdb_identity()["revision"],
            "status": "passed",
        }],
    )
    write_completion(
        workflow,
        "complete_store",
        site=workflow.completion_site,
        outputs=[child.store_dir / "manifest.json", report],
        validation={
            "check": "complete_store",
            "status": "passed",
            "files_status": "passed",
            "source_store_uri": str(source),
            "store_uri": str(child.store_dir),
            "n_analyses": summary.get("n_analyses", len(actual)),
            "n_variants": summary.get("n_variants", ""),
            "n_imputed": summary.get("n_imputed", ""),
            "completion_state": info.get("completion_state", ""),
            "resumed": checkpoint is not None,
            "completion_wall_seconds": f"{wall_seconds:.3f}",
        },
        command=command,
        extra_inputs=[
            workflow.completion_site.completion("register_completed_release"),
            workflow.paths.completion("validate_observed_release"),
        ],
    )


PHASES = {
    "validate_fixed_inputs": phase_validate_fixed_inputs,
    "resolve_analysis_metadata": phase_resolve_analysis_metadata,
    "build_observed_store": phase_build_observed_store,
    "build_observed_rho": phase_build_observed_rho,
    "regenerate_observed_overview": phase_regenerate_observed_overview,
    "validate_observed_release": phase_validate_observed_release,
    "register_completed_release": phase_register_completed_release,
    "complete_store": phase_complete_store,
    "build_completed_rho": phase_build_completed_rho,
    "regenerate_completed_overview": phase_regenerate_completed_overview,
    "validate_completed_release": phase_validate_completed_release,
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
