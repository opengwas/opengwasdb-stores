"""Release registration: assemble validation.yaml, verify planned vs executed argv, and finalize release.

Governed by ADR 0022 (flat opaque store IDs) and ADR 0023 (the seam is a command line).

Responsibilities:
1. Assembles `validation.yaml` and `records/register.json` from step records.
2. Compares each record's executed argv against `plan(bundle)`'s planned argv and fails
   on drift, naming the exact difference.
   - Staging normalization applies: executed argv contains `store.opengwasdb.partial` where
     planned argv contains `store.opengwasdb`.
   - Legitimate divergence: `complete-dense-resume` substituted for a planned `complete-dense`
     is accepted and recorded as `resumed: true` in `validation.yaml`.
3. Harvests observed measurements from step records:
   `format_version`, `n_variants`, `n_analyses`, `n_associations`, `store_bytes`,
   `build_elapsed_s`, and `validate_status`.
4. Atomic write: `validation.yaml` is written atomically only by `register`, so failed
   runs leave any previous `validation.yaml` intact.
5. Strict seam compliance: `register` opens NO Store and re-runs NO validation (ADR 0023).
6. Atomic publication: invokes `run.publish_store()` upon successful registration.

See docs/spec/store-release-workflow.md and ADRs 0022, 0023.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

from ogstores import bundle, paths, run
from ogstores.bundle import Bundle
from ogstores.plan import Step, plan


class RegisterError(RuntimeError):
    """Base error for release registration failures."""

    pass


class MissingRecordError(RegisterError, FileNotFoundError):
    """Raised when an expected Step execution record is missing."""

    pass


class StepFailedError(RegisterError):
    """Raised when a Step execution record reports failure."""

    pass


class ArgvDriftError(RegisterError, ValueError):
    """Raised when an executed argv drifted from the planned argv."""

    pass


def normalize_executed_argv_for_staging(
    argv: list[str],
    store_id: str,
    artifact_root: Path | str = paths.DEFAULT_ARTIFACT_ROOT,
) -> list[str]:
    """Normalize executed argv by replacing store.opengwasdb.partial with store.opengwasdb."""
    partial_token = str(paths.partial_store_path(store_id, root=artifact_root))
    target_token = str(paths.store_path(store_id, root=artifact_root))
    normalized: list[str] = []
    for token in argv:
        if token == partial_token:
            normalized.append(target_token)
        elif partial_token in token:
            normalized.append(token.replace(partial_token, target_token))
        elif "store.opengwasdb.partial" in token:
            normalized.append(token.replace("store.opengwasdb.partial", "store.opengwasdb"))
        else:
            normalized.append(token)
    return normalized


def check_argv_drift(
    planned_step: Step,
    executed_record: dict[str, Any],
    store_id: str,
    artifact_root: Path | str = paths.DEFAULT_ARTIFACT_ROOT,
) -> tuple[bool, bool]:
    """Compare planned argv against executed argv, allowing complete-dense-resume substitution.

    Returns:
        tuple[is_match, is_resumed]
    Raises:
        ArgvDriftError: If executed argv drifted from planned argv.
    """
    planned_argv = list(planned_step.argv)
    executed_argv = list(executed_record.get("argv") or [])

    # Legitimate divergence check: complete-dense-resume for planned complete-dense
    if (
        planned_step.name == "complete"
        and len(planned_argv) > 1
        and planned_argv[1] == "complete-dense"
        and len(executed_argv) > 1
        and executed_argv[1] == "complete-dense-resume"
    ):
        return True, True

    norm_executed = normalize_executed_argv_for_staging(
        executed_argv,
        store_id,
        artifact_root=artifact_root,
    )

    if norm_executed != planned_argv:
        planned_str = " ".join(planned_argv)
        executed_str = " ".join(executed_argv)
        norm_str = " ".join(norm_executed)
        raise ArgvDriftError(
            f"Step {planned_step.name!r} for {store_id} executed argv drifted from planned argv.\n"
            f"  Planned:  {planned_str}\n"
            f"  Executed: {executed_str}\n"
            f"  Normalized: {norm_str}"
        )

    return True, False


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    """Extract a JSON dictionary from lines of stdout/stderr if present."""
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


def harvest_observed_measurements(
    planned_steps: list[Step],
    step_records: dict[str, dict[str, Any]],
    bundle_obj: Bundle,
    *,
    is_resumed: bool = False,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Harvest observed measurements from step records without opening any Store (ADR 0023)."""
    total_elapsed = sum(
        float(rec.get("elapsed_seconds", 0.0))
        for rec in step_records.values()
    )

    format_version = "1.0"
    n_variants: int | None = None
    n_analyses: int | None = None
    n_associations: int | None = None
    store_bytes: int | None = None
    validate_status = "passed"

    # 1. Harvest from build or complete record
    producing_step = next((s.name for s in planned_steps if s.name in ("build", "complete")), None)
    if producing_step and producing_step in step_records:
        rec = step_records[producing_step]
        stdout_json = _extract_json_from_text(rec.get("stdout", ""))
        if stdout_json:
            if "n_variants" in stdout_json and isinstance(stdout_json["n_variants"], int):
                n_variants = stdout_json["n_variants"]
            if "n_analyses" in stdout_json and isinstance(stdout_json["n_analyses"], int):
                n_analyses = stdout_json["n_analyses"]
            if "n_associations" in stdout_json and isinstance(stdout_json["n_associations"], int):
                n_associations = stdout_json["n_associations"]
            if "format_version" in stdout_json and isinstance(stdout_json["format_version"], str):
                format_version = stdout_json["format_version"]
        else:
            stdout_text = rec.get("stdout", "")
            m_var = re.search(r"(\d+)\s+variants", stdout_text)
            if m_var:
                n_variants = int(m_var.group(1))
            m_ana = re.search(r"(\d+)\s+analyses", stdout_text)
            if m_ana:
                n_analyses = int(m_ana.group(1))

    # 2. Harvest from validate record
    if "validate" in step_records:
        val_rec = step_records["validate"]
        val_exit = val_rec.get("exit_code", 0)
        val_stdout = val_rec.get("stdout", "")
        val_stderr = val_rec.get("stderr", "")

        val_json = _extract_json_from_text(val_stdout)
        if val_json:
            if "status" in val_json:
                validate_status = str(val_json["status"])
            elif "valid" in val_json:
                validate_status = "passed" if val_json["valid"] else "failed"
            if "format_version" in val_json:
                format_version = str(val_json["format_version"])
            if "n_variants" in val_json and isinstance(val_json["n_variants"], int):
                n_variants = val_json["n_variants"]
            if "n_analyses" in val_json and isinstance(val_json["n_analyses"], int):
                n_analyses = val_json["n_analyses"]
            if "n_associations" in val_json and isinstance(val_json["n_associations"], int):
                n_associations = val_json["n_associations"]
            if "store_bytes" in val_json and isinstance(val_json["store_bytes"], int):
                store_bytes = val_json["store_bytes"]
        else:
            if val_exit != 0:
                validate_status = "failed"
            elif "warning" in val_stdout.lower() or "warning" in val_stderr.lower():
                validate_status = "passed_with_warnings"
            else:
                validate_status = "passed"

    # Fallback for n_analyses from the derived build manifest, if the builder did
    # not print it. Count the built manifest, never the bundle's audit table:
    # the bundle retains `exclude_from_build` rows that the build skipped, so
    # counting it would over-count by exactly those rows (ADR 0025).
    if n_analyses is None and manifest_path is not None and manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                lines = [l for l in f if l.strip()]
                n_analyses = max(0, len(lines) - 1)
        except Exception:
            pass

    # Fallback for n_associations on dense layout
    if n_associations is None and n_variants is not None and n_analyses is not None:
        if bundle_obj.layout == "dense":
            n_associations = n_variants * n_analyses

    observed: dict[str, Any] = {
        "format_version": format_version,
        "n_analyses": n_analyses,
        "n_variants": n_variants,
        "n_associations": n_associations,
        "store_bytes": store_bytes,
        "build_elapsed_s": round(total_elapsed, 3),
        "validate_status": validate_status,
    }
    if is_resumed:
        observed["resumed"] = True

    return observed


def _write_yaml_atomically(data: dict[str, Any], dest_path: Path) -> None:
    """Atomically write data to dest_path using a temporary file and os.replace."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(f".{dest_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    payload = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, dest_path)
    run._fsync_dir(dest_path.parent)


def register_release(
    bundle_input: Bundle | str,
    *,
    registry_root: Path | str | None = None,
    artifact_root: Path | str | None = None,
    force: bool = False,
    publish: bool = True,
) -> dict[str, Any]:
    """Assemble validation.yaml, verify executed vs planned argv, publish store, and record register.json.

    Parameters:
        bundle_input: Release Bundle instance or store_id string (e.g. 'OGS-00042').
        registry_root: Optional registry root path (default: stores/).
        artifact_root: Optional artifact root path override.
        force: If True, permit replacing an existing final Store during terminal publication.
        publish: If True, atomically publish staging store (.partial -> final store).

    Returns:
        dict containing the assembled validation.yaml data.
    Raises:
        MissingRecordError: If an expected step record is missing.
        StepFailedError: If a step record indicates a failed step.
        ArgvDriftError: If executed argv differs from planned argv.
    """
    if isinstance(bundle_input, str):
        b = bundle.load(bundle_input, registry_root=registry_root)
    else:
        b = bundle_input

    store_id = b.store_id
    paths.require_valid_store_id(store_id)

    if artifact_root is None:
        build_artifacts = b.build.get("artifacts")
        if isinstance(build_artifacts, dict) and "root" in build_artifacts:
            resolved_root = Path(build_artifacts["root"])
        else:
            resolved_root = paths.DEFAULT_ARTIFACT_ROOT
    else:
        resolved_root = Path(artifact_root)

    planned_steps = plan(b, artifact_root=resolved_root)
    step_records: dict[str, dict[str, Any]] = {}
    any_resumed = False

    # 1. Load and verify each planned step's record and check argv drift
    for step in planned_steps:
        rec = run.load_record(store_id, step.name, root=resolved_root)
        if rec is None:
            rec_p = paths.record_path(store_id, step.name, root=resolved_root)
            raise MissingRecordError(
                f"Cannot register release {store_id}: missing required execution record for "
                f"step {step.name!r} at {rec_p}"
            )
        if not rec.get("success") or rec.get("exit_code") != 0:
            raise StepFailedError(
                f"Cannot register release {store_id}: step {step.name!r} failed with "
                f"exit code {rec.get('exit_code')}"
            )

        _, is_resumed = check_argv_drift(step, rec, store_id, artifact_root=resolved_root)
        if is_resumed:
            any_resumed = True
        step_records[step.name] = rec

    # 2. Harvest observed measurements across all step records
    observed = harvest_observed_measurements(
        planned_steps,
        step_records,
        b,
        is_resumed=any_resumed,
        manifest_path=paths.build_manifest_path(store_id, root=resolved_root),
    )

    # 3. Assemble validation.yaml dictionary
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    val_status = observed.get("validate_status", "passed")

    # Determine overall status
    overall_status = "passed"
    if val_status == "failed":
        overall_status = "failed"
    elif val_status == "passed_with_warnings":
        overall_status = "passed_with_warnings"

    ogdb_exe = run.get_opengwasdb_executable()
    ogdb_rev = run.get_opengwasdb_revision(executable=ogdb_exe)
    ogdb_ver = run.get_opengwasdb_version()

    # Base dictionary merges existing Phase B checks/reports if present
    existing_val = b.validation or {}

    validation_data: dict[str, Any] = {
        "status": overall_status,
        "validated_at": now_iso,
        "validator": {
            "name": "opengwasdb validate",
            "version": f"opengwasdb@{ogdb_rev}" if ogdb_rev != run.UNAVAILABLE else f"opengwasdb v{ogdb_ver}",
        },
        "build_environment": {
            "opengwasdb_version": ogdb_ver,
            "opengwasdb_commit": ogdb_rev,
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "observed": observed,
        "checks": existing_val.get("checks", {
            "schema": "passed",
            "files": "passed",
            "store": val_status,
        }),
    }

    if "reports" in existing_val:
        validation_data["reports"] = existing_val["reports"]
    if "warnings" in existing_val:
        validation_data["warnings"] = existing_val["warnings"]
    if "errors" in existing_val:
        validation_data["errors"] = existing_val["errors"]

    # 4. Atomically publish store if publish=True
    published_p_str: str | None = None
    if publish:
        partial_p = paths.partial_store_path(store_id, root=resolved_root)
        target_p = paths.store_path(store_id, root=resolved_root)
        if partial_p.is_dir():
            published_target = run.publish_store(store_id, artifact_root=resolved_root, force=force)
            published_p_str = str(published_target)
        elif target_p.is_dir():
            published_p_str = str(target_p)

    # 5. Atomically write validation.yaml into stores/<store_id>/validation.yaml
    val_yaml_path = b.root / "validation.yaml"
    _write_yaml_atomically(validation_data, val_yaml_path)

    # 6. Atomically write records/register.json
    reg_rec_p = paths.record_path(store_id, "register", root=resolved_root)
    input_record_paths = [str(paths.record_path(store_id, s.name, root=resolved_root)) for s in planned_steps]
    register_result = {
        "step": "register",
        "store_id": store_id,
        "exit_code": 0,
        "success": True,
        "start_time": now_iso,
        "end_time": now_iso,
        "elapsed_seconds": 0.0,
        "argv": [],
        "planned_argv": [],
        "inputs": input_record_paths,
        "outputs": [str(val_yaml_path)],
        "opengwasdb_rev": ogdb_rev,
        "opengwasdb_version": ogdb_ver,
        "opengwasdb_executable": ogdb_exe,
        "stdout": "",
        "stderr": "",
        "record_path": str(reg_rec_p),
        "published_store": published_p_str,
    }
    run._write_record_atomically(register_result, reg_rec_p)

    return validation_data


__all__ = [
    "ArgvDriftError",
    "MissingRecordError",
    "RegisterError",
    "StepFailedError",
    "check_argv_drift",
    "harvest_observed_measurements",
    "normalize_executed_argv_for_staging",
    "register_release",
]
