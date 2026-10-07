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
   The verdict, `checks`, `warnings` and `errors` are this run's alone: they come
   from `opengwasdb validate --format json`, and nothing is carried from the
   Validation Record being replaced (#195). A candidate's Phase B evidence is
   kept apart, in a dated `acceptance` block that feeds none of them.
4. Atomic write: `validation.yaml` is written atomically only by `register`, so failed
   runs leave any previous `validation.yaml` intact.
5. Strict seam compliance: `register` opens NO Store and re-runs NO validation (ADR 0023).
6. Atomic publication: invokes `run.publish_store()` upon successful registration.

See docs/spec/store-release-workflow.md and ADRs 0022, 0023.
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

from ogstores import bundle, paths, run
from ogstores.bundle import Bundle
from ogstores.plan import Step, plan

# The Validation Record shape `register` writes. Every record it writes carries
# every `OBSERVED_FIELDS` and `BUILD_ENVIRONMENT_FIELDS` key, with a measurement
# it could not harvest recorded as None rather than omitted, defaulted or
# guessed (issues #122, #135). The `validation-record` tests assert both the
# committed records and the `register` output against these tuples and against
# `VALIDATOR_NAME`, so the format documented in `docs/release-metadata-schema.md`
# and the format produced here cannot drift apart.
VALIDATOR_NAME = "opengwasdb validate"
OBSERVED_FIELDS: tuple[str, ...] = (
    "format_version",
    "n_analyses",
    "n_variants",
    "n_associations",
    "store_bytes",
    "build_elapsed_s",
    "validate_status",
)
BUILD_ENVIRONMENT_FIELDS: tuple[str, ...] = (
    "opengwasdb_version",
    "opengwasdb_commit",
    "python_version",
    "platform",
)


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


class ValidateVerdictError(RegisterError, ValueError):
    """Raised when the validate step's record carries no `--format json` verdict."""

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


def normalize_executed_argv_for_variant_reference(
    argv: list[str], planned_step: Step,
) -> list[str]:
    """Normalize a variant-reference step's staged `--output-path` back to its destination.

    The executor writes the artifact to a staged sibling and renames it into
    place, so its record carries the staged path while the planned argv names
    the declared destination. Everything else about the argv must match exactly.
    """
    if planned_step.name != run.VARIANT_REFERENCE_STEP or not planned_step.outputs:
        return list(argv)
    target = str(planned_step.outputs[0])
    staged = str(run.variant_reference_partial_path(planned_step.outputs[0]))
    return [token.replace(staged, target) if staged in token else token for token in argv]


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
    norm_executed = normalize_executed_argv_for_variant_reference(
        norm_executed, planned_step
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


def validate_verdict(validate_record: dict[str, Any] | None) -> tuple[str, list[str], list[str]]:
    """This run's validate verdict, warnings and errors, read from `--format json`.

    `opengwasdb validate --format json` prints one object, `{"ok", "errors",
    "warnings"}` (opengwasdb#175). The verdict follows from it exactly: `failed`
    when not ok, `passed_with_warnings` when it reported a warning, `passed`
    otherwise. A plan without a validate step has no verdict, so it is
    `not_run` rather than an assumed pass.

    Text output is refused rather than searched for the word "warning" (#195):
    a record that does not carry the JSON verdict was not produced by the
    planned argv, and guessing its verdict would be a wrong answer that looks
    like a right one.
    """
    if validate_record is None:
        return "not_run", [], []
    payload = _extract_json_from_text(validate_record.get("stdout", ""))
    if (
        payload is None
        or not isinstance(payload.get("ok"), bool)
        or not isinstance(payload.get("errors"), list)
        or not isinstance(payload.get("warnings"), list)
    ):
        raise ValidateVerdictError(
            "The validate record carries no `opengwasdb validate --format json` verdict "
            "(an object with boolean `ok` and list `errors` and `warnings`) on stdout: "
            f"{validate_record.get('record_path') or 'records/validate.json'}"
        )
    errors = [str(e) for e in payload["errors"]]
    warnings = [str(w) for w in payload["warnings"]]
    if not payload["ok"]:
        return "failed", warnings, errors
    if warnings:
        return "passed_with_warnings", warnings, errors
    return "passed", warnings, errors


def committed_revision(path: Path | str) -> str | None:
    """The commit `path` was last changed in, or None when the file is not that commit's.

    None covers a path outside a git repository, an untracked file, and a file
    modified (staged or not) since its last commit: in each case no commit
    describes what is on disk. The workflow calls this before `register_release`,
    which itself spawns no subprocess (ADR 0023).
    """
    record_p = Path(path)
    if not record_p.is_file():
        return None

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(record_p.parent), *args, "--", record_p.name],
            capture_output=True,
            text=True,
        )

    try:
        if git("ls-files", "--error-unmatch").returncode != 0:
            return None
        if git("diff", "--quiet", "HEAD").returncode != 0:
            return None
        log = git("log", "-1", "--format=%H")
    except OSError:
        return None
    revision = log.stdout.strip()
    return revision if log.returncode == 0 and run.is_exact_commit_hash(revision) else None


def acceptance_evidence(
    previous: Mapping[str, Any] | None, *, commit: str | None = None
) -> dict[str, Any] | None:
    """The Phase B acceptance evidence a new Validation Record keeps, apart from its findings.

    A candidate record, one a Manifest Generator wrote rather than `register`,
    gives its `checks`, `warnings` and `reports` verbatim, dated by its own
    `validated_at` and tied to `commit`, the commit it came from (#195). A
    `register`-written record passes on the `acceptance` block it already
    carries, because the accepted bundle it describes has not changed. A
    `register`-shape record without one (the records #135 migrated) mixes
    Phase B evidence with an earlier build's findings, so nothing is extracted
    from it.

    The block never feeds the record's `status`, `checks.store`, `warnings`
    or `errors`.
    """
    if not isinstance(previous, Mapping):
        return None
    validator = previous.get("validator")
    written_by = validator.get("name") if isinstance(validator, Mapping) else None
    if written_by != VALIDATOR_NAME:
        return {
            "recorded_at": previous.get("validated_at"),
            "commit": commit,
            "checks": copy.deepcopy(previous.get("checks")),
            "warnings": copy.deepcopy(previous.get("warnings")),
            "reports": copy.deepcopy(previous.get("reports")),
        }
    carried = previous.get("acceptance")
    return copy.deepcopy(dict(carried)) if isinstance(carried, Mapping) else None


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

    # Only a step that printed it can report the format version; none is
    # assumed, because 0.1.0 Stores were registered as "1.0" (#135, #195).
    format_version: str | None = None
    n_variants: int | None = None
    n_analyses: int | None = None
    n_associations: int | None = None
    store_bytes: int | None = None
    validate_status, _, _ = validate_verdict(step_records.get("validate"))

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

    # 2. Harvest measurements from the validate record; its verdict is above
    if "validate" in step_records:
        val_json = _extract_json_from_text(step_records["validate"].get("stdout", ""))
        if val_json:
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

    # Variant-reference provenance (issue #148). A declared pre-stage records
    # whether this run extracted the artifact or found it already provided; a
    # build option naming an artifact with no declared pre-stage (OGS-00004/5)
    # records it as provided. A release that uses no variant reference carries
    # no key at all, matching the existing conditional `resumed` note.
    variant_reference_record = step_records.get("variant-reference")
    if variant_reference_record is not None:
        observed["variant_reference"] = (
            "provided" if variant_reference_record.get("skipped") is True else "extracted"
        )
    else:
        phase = "complete" if bundle_obj.completion_state == "reference_completed" else "build"
        phase_options = (bundle_obj.build.get(phase) or {}).get("options") or {}
        if "variant-reference" in phase_options:
            observed["variant_reference"] = "provided"

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
    acceptance_commit: str | None = None,
) -> dict[str, Any]:
    """Assemble validation.yaml, verify executed vs planned argv, publish store, and record register.json.

    Parameters:
        bundle_input: Release Bundle instance or store_id string (e.g. 'OGS-00042').
        registry_root: Optional registry root path (default: stores/).
        artifact_root: Optional artifact root path override.
        force: If True, permit replacing an existing final Store during terminal publication.
        publish: If True, atomically publish staging store (.partial -> final store).
        acceptance_commit: The commit of the bundle's current validation.yaml
            (`committed_revision`), recorded when that is a candidate record.

    Returns:
        dict containing the assembled validation.yaml data.
    Raises:
        MissingRecordError: If an expected step record is missing.
        StepFailedError: If a step record indicates a failed step.
        ArgvDriftError: If executed argv differs from planned argv.
        ValidateVerdictError: If the validate record carries no `--format json` verdict.
    """
    if isinstance(bundle_input, str):
        b = bundle.load(bundle_input, registry_root=registry_root)
    else:
        b = bundle_input

    store_id = b.store_id
    paths.require_valid_store_id(store_id)

    # Deployment configuration, not a Build Recipe fact (issue #126).
    resolved_root = (
        Path(artifact_root) if artifact_root is not None else paths.artifact_root()
    )

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

    # 2. This run's verdict and findings, then its observed measurements
    val_status, val_warnings, val_errors = validate_verdict(step_records.get("validate"))
    acceptance = acceptance_evidence(b.validation, commit=acceptance_commit)
    observed = harvest_observed_measurements(
        planned_steps,
        step_records,
        b,
        is_resumed=any_resumed,
        manifest_path=paths.build_manifest_path(store_id, root=resolved_root),
    )

    # 3. Assemble validation.yaml dictionary
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    ogdb_exe = run.get_opengwasdb_executable()
    ogdb_rev = run.get_opengwasdb_revision(executable=ogdb_exe)
    ogdb_ver = run.get_opengwasdb_version()

    # Only this run's findings. The bundle's previous validation.yaml is
    # replaced, never merged: its checks, warnings, errors and reports describe
    # another run, and republishing them under this run's validated_at is the
    # wrong answer that looks right (#195). Phase B acceptance evidence survives
    # only in its own labelled `acceptance` block, added below.
    validation_data: dict[str, Any] = {
        "status": val_status,
        "validated_at": now_iso,
        "validator": {
            "name": VALIDATOR_NAME,
            "version": f"opengwasdb@{ogdb_rev}" if ogdb_rev != run.UNAVAILABLE else f"opengwasdb v{ogdb_ver}",
        },
        "build_environment": {
            "opengwasdb_version": ogdb_ver,
            "opengwasdb_commit": ogdb_rev,
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "observed": observed,
        "checks": {"store": val_status},
        "warnings": val_warnings,
        "errors": val_errors,
    }
    if acceptance is not None:
        validation_data["acceptance"] = acceptance

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
    "BUILD_ENVIRONMENT_FIELDS",
    "OBSERVED_FIELDS",
    "VALIDATOR_NAME",
    "ArgvDriftError",
    "MissingRecordError",
    "RegisterError",
    "StepFailedError",
    "ValidateVerdictError",
    "acceptance_evidence",
    "check_argv_drift",
    "committed_revision",
    "harvest_observed_measurements",
    "normalize_executed_argv_for_staging",
    "normalize_executed_argv_for_variant_reference",
    "register_release",
    "validate_verdict",
]
