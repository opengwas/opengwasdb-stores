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
import shutil
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


class PublicationError(RegisterError):
    """Raised when a pending publication's state matches no step of its transaction."""

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


# The object `opengwasdb validate --format json` prints at the pinned revision:
# {"ok": bool, "errors": [str], "warnings": [str]}, with ok == (not errors), and
# exit 1 exactly when not ok (opengwasdb#175; cli/main.py validate_command).
VALIDATE_JSON_KEYS: frozenset[str] = frozenset({"ok", "errors", "warnings"})


def validate_verdict(validate_record: dict[str, Any] | None) -> tuple[str, list[str], list[str]]:
    """This run's validate verdict, warnings and errors, read from `--format json`.

    The record's stdout must be exactly the one JSON object the pinned CLI
    prints, `{"ok", "errors", "warnings"}`, with nothing before or after it, no
    other key, a boolean `ok`, and lists of strings. It must also agree with
    itself and with the step: `ok` is true exactly when `errors` is empty, and
    the step, whose `exit_code` must be an int and `success` a bool, exited 0
    and succeeded exactly when `ok` (#195). Then the verdict
    is `failed` when not ok, `passed_with_warnings` when it reported a warning,
    and `passed` otherwise. A plan without a validate step has no verdict, so it
    is `not_run` rather than an assumed pass.

    Anything else is refused with `ValidateVerdictError`, never coerced or
    searched for the word "warning": a record the pinned CLI could not have
    written would otherwise become a wrong answer that looks like a right one.
    """
    if validate_record is None:
        return "not_run", [], []
    where = validate_record.get("record_path") or "records/validate.json"

    def refuse(reason: str) -> ValidateVerdictError:
        return ValidateVerdictError(
            f"The validate record {where} does not carry the verdict `opengwasdb validate "
            f"--format json` prints: {reason}"
        )

    stdout = validate_record.get("stdout")
    if not isinstance(stdout, str):
        raise refuse("its stdout is not text")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise refuse(f"its stdout is not exactly one JSON object ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise refuse(f"its stdout is a JSON {type(payload).__name__}, not an object")
    if set(payload) != VALIDATE_JSON_KEYS:
        raise refuse(f"expected exactly the keys {sorted(VALIDATE_JSON_KEYS)}, got {sorted(payload)}")
    ok, errors, warnings = payload["ok"], payload["errors"], payload["warnings"]
    if not isinstance(ok, bool):
        raise refuse(f"`ok` is {ok!r}, not a boolean")
    for name, items in (("errors", errors), ("warnings", warnings)):
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise refuse(f"`{name}` is {items!r}, not a list of strings")
    if ok != (not errors):
        raise refuse(f"`ok` is {ok} with {len(errors)} error(s); the CLI sets ok exactly when there are none")
    # The record's own fields must have the exact types run.py writes: an int
    # exit code (bool and float compare equal to 0 and 1, so they are refused by
    # type, not value) and a bool success.
    exit_code, success = validate_record.get("exit_code"), validate_record.get("success")
    if type(exit_code) is not int:
        raise refuse(f"its exit_code is {exit_code!r}, not an int")
    if type(success) is not bool:
        raise refuse(f"its success is {success!r}, not a bool")
    expected_exit = 0 if ok else 1
    if exit_code != expected_exit or success is not ok:
        raise refuse(
            f"`ok: {str(ok).lower()}` means exit code {expected_exit} and success {ok}, "
            f"but the record has exit code {exit_code!r} and success {success!r}"
        )
    if not ok:
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
    gives its `checks`, `warnings`, `reports`, `reference_overlap` and
    `store_composition` verbatim, dated by its own `validated_at` and tied to
    `commit`, the commit it came from (#195). A
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
            # OGS-00011's generator records its reference-overlap evidence as a
            # top-level block; it is named here, not swept up as "everything else".
            "reference_overlap": copy.deepcopy(previous.get("reference_overlap")),
            # The shared-Store MAF-filtered admission summary (#203) is another
            # named top-level block, never swept up as "everything else".
            "store_composition": copy.deepcopy(previous.get("store_composition")),
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

    # 2. The validate record carries only its verdict: its JSON has exactly the
    # keys `validate_verdict` accepts, so it reports no measurement (#195).

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
    payload = _dump_yaml(data)
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, dest_path)
    run._fsync_dir(dest_path.parent)


# The durable steps of a publication, in order: a crash between any two leaves
# a state `complete_publication` recognises and finishes (#195). Tests inject a
# crash at each through `run.fault_boundary`.
PUBLICATION_BOUNDARIES: tuple[str, ...] = (
    "publication-started",
    "marker-written",
    "store-set-aside",
    "store-published",
    "old-store-archived",
    "old-records-archived",
    "old-record-archived",
    "record-written",
    "register-record-written",
    "marker-removed",
)
_MARKER_KEYS: frozenset[str] = frozenset({
    "store_id",
    "archive",
    "snapshot",
    "bundle_validation",
    "previous_validation",
    "validation_yaml",
    "register_record",
    "written_at",
})


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def _write_text_atomically(text: str, dest_path: Path) -> None:
    """Atomically write text to dest_path using a temporary file and os.replace."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(f".{dest_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, dest_path)
    run._fsync_dir(dest_path.parent)


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _load_marker(store_id: str, root: Path) -> dict[str, Any] | None:
    marker_p = paths.publication_marker(store_id, root=root)
    if not _exists(marker_p):
        return None
    try:
        marker = json.loads(marker_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"Cannot read the pending publication {marker_p}: {exc}") from exc
    if not isinstance(marker, dict) or set(marker) != _MARKER_KEYS or marker.get("store_id") != store_id:
        raise PublicationError(f"{marker_p} is not a publication marker for {store_id}")
    store_dir_p = paths.store_dir(store_id, root=root)
    archive, snapshot = marker["archive"], marker["snapshot"]
    if archive is not None and Path(archive).parent != store_dir_p / paths.REPLACED_DIRNAME:
        raise PublicationError(f"{marker_p} names an archive outside {store_dir_p}: {archive}")
    if snapshot is not None and (
        Path(snapshot).parent != store_dir_p
        or not Path(snapshot).name.startswith(paths.FORCE_SNAPSHOT_PREFIX)
    ):
        raise PublicationError(f"{marker_p} names a snapshot outside {store_dir_p}: {snapshot}")
    return marker


def remove_publication_marker(store_id: str, root: Path | str) -> None:
    """Remove a finished publication's marker: always the transaction's last step (#195)."""
    marker_p = paths.publication_marker(store_id, root=root)
    marker_p.unlink()
    run._fsync_dir(marker_p.parent)
    run.fault_boundary("marker-removed")


def complete_publication(
    store_id: str, root: Path | str, *, finalize: bool = True
) -> dict[str, Any] | None:
    """Finish a pending publication from wherever it stopped (#195).

    `register_release` writes `publication.json` once it has verified every
    record and assembled the Validation Record, and then calls this function.
    The entry point calls it again for any marker a crash left behind. Each
    step checks what is already done, so the function is idempotent, and the
    marker is removed last, only when everything below agrees:

    1. The staged Store is published. For a replacement, the old Store is first
       set aside as `.backup`, and after the swap it is renamed into the
       archive, never deleted.
    2. For a replacement, the run's records snapshot moves into the archive as
       `records/`, and the replaced Validation Record is written there.
    3. The new `validation.yaml` and `records/register.json` are written from
       the marker.
    4. With `finalize`, the marker is removed. The entry point passes False and
       removes it itself, after Snakemake has finished the register job and
       any mark that a killed job left `register.json` incomplete is cleared.
       So a crash before then always leaves a marker for the next run.

    A state that fits no step, such as neither a Store nor a staged Store, is
    raised as `PublicationError` and the marker is kept. Returns the marker, or
    None when nothing was pending.
    """
    resolved_root = Path(root)
    marker = _load_marker(store_id, resolved_root)
    if marker is None:
        return None
    marker_p = paths.publication_marker(store_id, root=resolved_root)
    store_p = paths.store_path(store_id, root=resolved_root)
    partial_p = paths.partial_store_path(store_id, root=resolved_root)
    backup_p = paths.backup_store_path(store_id, root=resolved_root)
    store_dir_p = store_p.parent

    def stuck(state: str) -> PublicationError:
        return PublicationError(
            f"Cannot complete the publication of {store_id} ({marker_p}): {state}. "
            "Nothing was changed; the marker is kept for an operator."
        )

    if marker["archive"] is None:
        # A first publication: no Store is replaced.
        if not _exists(store_p):
            if not _exists(partial_p):
                raise stuck("neither the Store nor the staged Store exists")
            partial_p.rename(store_p)
            run._fsync_dir(store_dir_p)
            run.fault_boundary("store-published")
        elif _exists(partial_p):
            raise stuck("a Store and a staged Store both exist, but nothing is being replaced")
    else:
        archive = Path(marker["archive"])
        archived_store = archive / store_p.name
        if _exists(archived_store):
            if _exists(backup_p):
                raise stuck(f"both {archived_store} and {backup_p} exist")
            if not _exists(store_p):
                raise stuck(f"the old Store is archived but no Store is published at {store_p}")
        else:
            if not _exists(backup_p):
                if not (_exists(store_p) and _exists(partial_p)):
                    raise stuck("the Store to replace or its replacement is missing")
                store_p.rename(backup_p)
                run._fsync_dir(store_dir_p)
                run.fault_boundary("store-set-aside")
            if not _exists(store_p):
                if not _exists(partial_p):
                    raise stuck(f"the old Store is at {backup_p} but the staged Store is missing")
                partial_p.rename(store_p)
                run._fsync_dir(store_dir_p)
                run.fault_boundary("store-published")
            archive.mkdir(parents=True, exist_ok=True)
            backup_p.rename(archived_store)
            run._fsync_dir(archive)
            run._fsync_dir(store_dir_p)
            run.fault_boundary("old-store-archived")

        snapshot = Path(marker["snapshot"]) if marker["snapshot"] else None
        if snapshot is not None and _exists(snapshot):
            run.archive_force_snapshot(snapshot, archive)
            run.fault_boundary("old-records-archived")
        if marker["previous_validation"] is not None and not _exists(archive / "validation.yaml"):
            _write_text_atomically(marker["previous_validation"], archive / "validation.yaml")
            run.fault_boundary("old-record-archived")

    _write_text_atomically(marker["validation_yaml"], Path(marker["bundle_validation"]))
    run.fault_boundary("record-written")
    run._write_record_atomically(
        marker["register_record"], paths.record_path(store_id, "register", root=resolved_root)
    )
    run.fault_boundary("register-record-written")
    if finalize:
        remove_publication_marker(store_id, resolved_root)
    return marker


def register_release(
    bundle_input: Bundle | str,
    *,
    registry_root: Path | str | None = None,
    artifact_root: Path | str | None = None,
    force: bool = False,
    publish: bool = True,
    acceptance_commit: str | None = None,
    finalize: bool = True,
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
        finalize: If False, leave `publication.json` for the caller to remove.
            The entry point does so once Snakemake has finished this job (#195).

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

    marker_p = paths.publication_marker(store_id, root=resolved_root)
    if _exists(marker_p):
        raise run.PublicationPendingError(
            f"Cannot register {store_id}: the publication {marker_p} has not finished; "
            "`pixi run release` completes it first"
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

    # 4. Publication. Staging a Store is one transaction (#195): once everything
    # is verified, publication.json records the whole outcome, the new and old
    # Validation Records and register.json included, and complete_publication
    # carries it out. A crash at any point leaves the marker, so the next run
    # finishes the job; nothing below raises before the marker without leaving
    # the published release as it was.
    val_yaml_path = b.root / "validation.yaml"
    reg_rec_p = paths.record_path(store_id, "register", root=resolved_root)
    partial_p = paths.partial_store_path(store_id, root=resolved_root)
    target_p = paths.store_path(store_id, root=resolved_root)
    staged = publish and partial_p.is_dir()
    replacing = staged and _exists(target_p)
    if replacing and not force:
        raise run.StoreExistsError(f"Target store already exists at {target_p}. Set force=True to replace.")

    archive: Path | None = None
    snapshot: Path | None = None
    if replacing:
        # Replacing archives the old release under replaced/<stamp>/, with the
        # stamp of the forced run's records snapshot, so the two name each other.
        snapshots = run.pending_force_snapshots(store_id, resolved_root)
        if len(snapshots) > 1:
            raise RegisterError(
                f"Cannot replace {store_id}: more than one forced-run records snapshot: "
                + ", ".join(str(p) for p in snapshots)
            )
        snapshot = snapshots[0] if snapshots else None
        stamp = snapshot.name[len(paths.FORCE_SNAPSHOT_PREFIX):] if snapshot else run.utc_stamp()
        archive = paths.replaced_dir(store_id, stamp, root=resolved_root)
        if _exists(archive / target_p.name):
            raise FileExistsError(f"Cannot replace {store_id}: {archive / target_p.name} already exists")
        validation_data["replaced"] = {"archive": str(archive), "replaced_at": now_iso}

    if staged or (publish and target_p.is_dir()):
        published_p_str: str | None = str(target_p)
    else:
        published_p_str = None

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
        "replaced_archive": str(archive) if archive else None,
    }

    if staged:
        run._preflight_paths(store_id, resolved_root)
        val_rec = step_records.get("validate")
        if not val_rec or not val_rec.get("success") or val_rec.get("exit_code") != 0:
            raise RegisterError(
                f"Cannot publish {store_id}: missing successful 'validate' record in records/."
            )
        run.fault_boundary("publication-started")
        marker = {
            "store_id": store_id,
            "archive": str(archive) if archive else None,
            "snapshot": str(snapshot) if snapshot else None,
            "bundle_validation": str(val_yaml_path.resolve()),
            "previous_validation": (
                val_yaml_path.read_text(encoding="utf-8") if replacing and val_yaml_path.is_file() else None
            ),
            "validation_yaml": _dump_yaml(validation_data),
            "register_record": register_result,
            "written_at": now_iso,
        }
        run._write_record_atomically(marker, marker_p)
        run.fault_boundary("marker-written")
        complete_publication(store_id, resolved_root, finalize=finalize)
        return validation_data

    # Nothing staged: write the record beside whatever is (or is not) published.
    _write_yaml_atomically(validation_data, val_yaml_path)
    run._write_record_atomically(register_result, reg_rec_p)

    return validation_data


__all__ = [
    "BUILD_ENVIRONMENT_FIELDS",
    "OBSERVED_FIELDS",
    "VALIDATOR_NAME",
    "ArgvDriftError",
    "MissingRecordError",
    "PUBLICATION_BOUNDARIES",
    "PublicationError",
    "RegisterError",
    "StepFailedError",
    "ValidateVerdictError",
    "acceptance_evidence",
    "check_argv_drift",
    "committed_revision",
    "complete_publication",
    "harvest_observed_measurements",
    "normalize_executed_argv_for_staging",
    "normalize_executed_argv_for_variant_reference",
    "register_release",
    "remove_publication_marker",
    "validate_verdict",
]
