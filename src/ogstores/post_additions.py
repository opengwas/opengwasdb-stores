"""Add the post steps a published Store's recipe declares and its records lack, in place (#528, ADR 0043).

A Build Recipe that gains `post.rho` or `post.clumps` (or a standalone
`post.top_hits`) after its Store was published declares Store contents the
release does not have. They are re-runnable in-place additions (opengwasdb ADR
0061; ADR 0037, clarified by #305), so adding them is no rebuild. The owner
ruled that the rebuilt 0.3.0 Stores gain their Clumps this way
(opengwas/opengwasdb#420, 25 Nov 2026; #520 stream D).

    pixi run release OGS-00015          adds them, validates, publishes, registers
    pixi run release-dry OGS-00015      shows that plan; writes nothing

The entry point (`ogstores.release`) asks `assess` about each release it is
named, unforced, before Snakemake. A release is this mode's when its last
phase has published (`records/register.json`, or `completion-register.json`
for one that completes its own Store) and that phase lacks current evidence
for a `top-hits`, `rho`, `clump` or `overview` step: no successful record, or,
for an addable step, a record planned otherwise than the recipe now plans it
(a changed LD Store mapping). Its plan is those steps in plan order, the
declared `overview` whenever anything is added (the engine's overview reports
the Rho Matrix and the Clumps), then the phase's `validate` and its register.
A release whose completion is still to run is not this mode's: completion
comes first, and the post steps it declares run in its completion phase (ADR
0037). A release that also lacks any other step's record is left to
Snakemake, as before.

One call, under the release's run lock (`add_post_steps`):

1. refuses, before writing anything: a bundle that no longer plans the Store
   it published (`amendment.published_store_changes`), what a killed run or
   an unfinished completion left beside the Store, a Store path or staged path
   that is a symlink (`run`'s no-follow guard), a published Store that is not
   format 0.3.0, a runtime that is not the locked engine, and a filesystem that
   cannot reflink;
2. through `staged_publication.publish`, the lifecycle `publish_rewrite`
   shares: copies `records/` to a snapshot, stages the published Store at
   `store.opengwasdb.partial` by `cp --reflink=always` (never a full copy),
   runs the steps and `validate` there (`run.execute_step`), refuses a failed
   step, a validation error and, for a rewritten Store, any warning ADR 0042's
   deviation rule does not accept, then writes `publication.json` and finishes
   it: the old Store, its records and its Validation Record go under
   `replaced/<UTC>/`.

The new Validation Record keeps the published one's lineage: `observed`,
`acceptance` and any `rewrite` block are carried, and only what the run
measured changes. Until the marker exists, any failure removes the staged copy
and puts `records/` back. A release this mode published stays its: `assess`
refuses its drift from its records, and the Snakefile settles the edges between
its post steps (`current`), so mtimes do not reschedule them.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ogstores import amendment, bundle, paths, publish_rewrite, register, run, runtime, staged_publication
from ogstores.plan import (
    BUILD_PHASE,
    COMPLETION_PHASE,
    COMPLETION_STEP_PREFIX,
    REGISTER_STEPS,
    Step,
    plan,
)
from ogstores.runtime import REQUIRED_FORMAT_VERSION
from ogstores.store_info import store_info
from ogstores.store_size import store_size_on_disk

#: The post steps this mode adds, by their build-phase names; a completion
#: phase's are `completion-<name>`. A Dense build and every completion build
#: the Top-Hit Index inline, so they plan no `top-hits` step to add.
ADDABLE_STEPS: tuple[str, ...] = ("top-hits", "rho", "clump")

#: The step regenerated whenever anything is added, and added when its record is missing.
OVERVIEW_STEP: str = "overview"

#: The Validation Record and register record block that lists each addition.
BLOCK: str = "post_additions"

#: The test-only boundaries after the snapshot and after the staged copy exists (`run.fault_boundary`).
SNAPSHOT_BOUNDARY: str = "post-additions-snapshot-taken"
STAGED_BOUNDARY: str = "post-additions-staged"

#: The copy that stages the published Store: blocks are shared, never copied.
REFLINK_COPY: tuple[str, ...] = ("cp", "-a", "--reflink=always")


class PostAdditionRefused(register.RegisterError):
    """Adding post steps refused; the published Store, records/ and validation.yaml are as they were."""


def base_name(step_name: str) -> str:
    return step_name[len(COMPLETION_STEP_PREFIX):] if step_name.startswith(COMPLETION_STEP_PREFIX) else step_name


def is_addable(step_name: str) -> bool:
    return base_name(step_name) in ADDABLE_STEPS


def _is_overview(step_name: str) -> bool:
    return base_name(step_name) == OVERVIEW_STEP


@dataclass(frozen=True)
class Assessment:
    """What `assess` found for a published release: the steps to run, in plan order (none: up to date)."""

    bundle: bundle.Bundle
    root: Path
    phase: str
    phase_steps: list[Step]
    #: The post steps this run adds or regenerates, `validate` excluded.
    missing: list[Step]
    validate: Step
    #: Whether any of the phase's evidence is not current; `validate` reruns whenever it is.
    stale: bool
    register_step: str
    register_record: dict[str, Any] = field(repr=False)

    @property
    def store_id(self) -> str:
        return self.bundle.store_id

    @property
    def up_to_date(self) -> bool:
        return not self.stale

    @property
    def steps(self) -> list[Step]:
        return [*self.missing, self.validate] if self.stale else []

    def records(self) -> list[Path]:
        """The phase's records and its register's, as this run leaves them."""
        names = [step.name for step in self.phase_steps] + [self.register_step]
        return [paths.record_path(self.store_id, name, root=self.root) for name in names]

    def plan_lines(self) -> list[str]:
        """The run's plan, one step per line, as the dry run shows it."""
        return [
            *(f"  {step.name}: {' '.join(step.argv)}" for step in self.steps),
            f"  {self.register_step}: publish the staged copy, replacing the Store (archived under "
            f"{paths.replaced_dir(self.store_id, '<UTC>', root=self.root)})",
        ]


def _record(path: Path) -> dict[str, Any] | None:
    try:
        return run.read_record(path)
    except ValueError as exc:
        raise PostAdditionRefused(str(exc)) from exc


def _succeeded(record: Mapping[str, Any] | None) -> bool:
    return record is not None and record.get("success") is True and record.get("exit_code") == 0


def _publishing_phase(b: bundle.Bundle, root: Path) -> str | None:
    """The phase whose register record describes the published Store, or None.

    None when nothing is published, and when a release that completes its own
    Store has published only its build phase: completion comes first (ADR 0037).
    """
    if os.path.lexists(paths.record_path(b.store_id, REGISTER_STEPS[COMPLETION_PHASE], root=root)):
        return COMPLETION_PHASE
    if not os.path.lexists(paths.record_path(b.store_id, REGISTER_STEPS[BUILD_PHASE], root=root)):
        return None
    return None if b.completes_own_store else BUILD_PHASE


def _planned_as_now(b: bundle.Bundle, phase: str, step: Step, record: Mapping[str, Any], root: Path) -> bool:
    """Whether the record planned `step` as the recipe plans it now, at the root it was written under."""
    record_root = amendment._record_root(dict(record), b.store_id, root)
    if record_root != root:
        step = next((s for s in plan(b, artifact_root=record_root) if s.phase == phase and s.name == step.name), step)
    return record.get("planned_argv") == step.argv


def assess(b: bundle.Bundle, root: Path | str) -> Assessment | None:
    """This mode's view of a release, read from its records and bundle; None when it is not the mode's.

    Evidence is current when the step's record succeeded and, for an addable
    step or `validate`, planned the step as the recipe plans it now. It is the
    mode's when its last phase has published, its Store path exists, and the
    phase lacks current evidence only for addable steps, `overview` and
    `validate`: at least one addable or `overview`, or any of them once this
    mode has published it (`BLOCK` in its register record). With nothing
    lacking and `BLOCK` present it is up to date. Raises `PostAdditionRefused`
    when a release that is the mode's has a register record that did not
    succeed, or a bundle that no longer plans the Store it published (a
    changed Store-writing argv or membership), with or without missing steps:
    the decision is this function's, never Snakemake's mtimes.
    """
    root = Path(root)
    store_id = b.store_id
    phase = _publishing_phase(b, root)
    if phase is None or not os.path.lexists(paths.store_path(store_id, root=root)):
        return None
    register_step = REGISTER_STEPS[phase]
    register_record = _record(paths.record_path(store_id, register_step, root=root))
    if not isinstance(register_record, dict):
        return None
    phase_steps = [step for step in plan(b, artifact_root=root) if step.phase == phase]
    validate_name = register.phase_validate_step(phase_steps)
    validate = next((step for step in phase_steps if step.name == validate_name), None)
    if validate is None:
        return None

    def current(step: Step) -> bool:
        record = _record(paths.record_path(store_id, step.name, root=root))
        if not _succeeded(record):
            return False
        if is_addable(step.name) or step.name == validate_name:
            return _planned_as_now(b, phase, step, record, root)  # type: ignore[arg-type]
        return True

    lacking = [step for step in phase_steps if not current(step)]
    if any(not (is_addable(s.name) or _is_overview(s.name) or s.name == validate_name) for s in lacking):
        return None  # Snakemake's, as before: this mode adds only post steps
    added = [s for s in lacking if is_addable(s.name) or _is_overview(s.name)]
    if not added and BLOCK not in register_record:
        return None
    # The engine's overview reports the Rho Matrix and the Clumps: a declared
    # one is regenerated whenever anything is added.
    missing = [s for s in phase_steps if s in added or (_is_overview(s.name) and any(is_addable(a.name) for a in added))]
    # From here the release is this mode's, so its evidence must hold, whatever Snakemake would schedule.
    changes = [] if _succeeded(register_record) else [
        f"{store_id}: its register record {register_step}.json records no successful registration "
        f"(exit code {register_record.get('exit_code')!r}, success {register_record.get('success')!r})"
    ]
    changes += amendment.published_store_changes(b, root)
    if changes:
        listed = "\n".join(f"  {change}" for change in changes)
        what = f"add {', '.join(s.name for s in missing)} to" if missing else "judge"
        raise PostAdditionRefused(
            f"Refusing to {what} {store_id}: its records no longer describe the Store its bundle plans "
            f"(ADR 0037, ADR 0043):\n{listed}\nNothing was written. "
            f"`pixi run release {store_id} --config force=1` rebuilds it from the bundle as it now stands."
        )
    return Assessment(
        bundle=b,
        root=root,
        phase=phase,
        phase_steps=phase_steps,
        missing=missing,
        validate=validate,
        stale=bool(lacking),
        register_step=register_step,
        register_record=register_record,
    )


def current(b: bundle.Bundle, root: Path | str) -> bool:
    """Whether this mode published the release and its evidence is all current (ADR 0043).

    The Snakefile then settles the edges between its post steps: a later
    step's record (an Indexed Variant Subset, an Observed-View Check) is older
    than the added one before it, and its mtime would reschedule it as a
    rebuild. A refusal, or anything it cannot read, is not current.
    """
    try:
        assessment = assess(b, root)
    except (register.RegisterError, OSError, ValueError):
        return False
    return assessment is not None and assessment.up_to_date


def filesystem_type(path: Path) -> str:
    """The filesystem `path` is on, as `stat -f` names it, or `unknown`."""
    result = subprocess.run(["stat", "-f", "-c", "%T", str(path)], capture_output=True, text=True, check=False)
    return result.stdout.strip() or "unknown"


def _reflink(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*REFLINK_COPY, str(source), str(destination)], capture_output=True, text=True, check=False)


def refuse_without_reflink(directory: Path) -> None:
    """Refuse a filesystem that cannot share blocks between copies, naming it; never fall back to a full copy.

    A probe file is reflinked beside the Store, in its release directory, then removed.
    """
    probe_dir = Path(tempfile.mkdtemp(prefix=".reflink-probe-", dir=directory))
    try:
        (probe_dir / "a").write_bytes(b"reflink probe\n")
        result = _reflink(probe_dir / "a", probe_dir / "b")
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    if result.returncode != 0:
        raise PostAdditionRefused(
            f"{directory} is on a {filesystem_type(directory)} filesystem, which `{' '.join(REFLINK_COPY)}` "
            f"cannot copy by reflink ({result.stderr.strip() or 'exit ' + str(result.returncode)}). The "
            "published Store is staged only by reflink, never by a full copy (ADR 0043). Nothing was written."
        )


def _refuse_leftovers(store_id: str, root: Path) -> None:
    """Refuse what a killed run or an unfinished completion left beside the Store, naming the remedy."""
    for leftover, what in (
        (paths.publication_marker(store_id, root=root), "a publication no run finished"),
        (paths.partial_store_path(store_id, root=root), "a staged Store no run is publishing"),
        (paths.backup_store_path(store_id, root=root), "a replaced Store no publication finished"),
        (paths.completion_checkpoint_path(store_id, root=root), "an unfinished completion's checkpoint"),
        (paths.completion_start_path(store_id, root=root), "an unfinished completion's start record"),
    ):
        if os.path.lexists(leftover):
            raise PostAdditionRefused(
                f"{leftover} exists: {what}. Finish or remove it before adding post steps to "
                f"{store_id}; a staged Store this mode left is a reflink copy, safe to delete. Nothing was written."
            )
    snapshots = run.pending_force_snapshots(store_id, root)
    if snapshots:
        raise PostAdditionRefused(
            f"{store_id} holds a records snapshot, {snapshots[0]}, that no publication finished: "
            f"`pixi run release {store_id} --resolve-snapshot restore|delete` resolves it. Nothing was written."
        )


def preflight(a: Assessment, *, registry_root: Path, lock: Path | None = None, probe: bool = True) -> dict[str, Any]:
    """Every check before the first write; returns the published Store's `info`.

    `probe=False` leaves out the reflink probe, which writes a file beside the
    Store: the dry run's checks write nothing.
    """
    store_id, root = a.store_id, a.root
    _refuse_leftovers(store_id, root)
    store_p = paths.store_path(store_id, root=root)
    try:
        # `run`'s no-follow guard: the release directory and every Store path in the transaction.
        run._preflight_paths(store_id, root)
    except ValueError as exc:
        raise PostAdditionRefused(f"{exc}. Nothing was written.") from exc
    if not store_p.is_dir():
        raise PostAdditionRefused(f"{store_id} has no published Store at {store_p}. Nothing was written.")
    try:
        published = store_info(store_p)
    except (OSError, ValueError) as exc:
        raise PostAdditionRefused(f"`opengwasdb info` cannot read the published Store {store_p}: {exc}") from exc
    format_version = published.get("format_version")
    if format_version != REQUIRED_FORMAT_VERSION:
        raise PostAdditionRefused(
            f"{store_id}'s published Store is format {format_version!r}; post steps are added in place "
            f"only to a {REQUIRED_FORMAT_VERSION} Store (Clumps exist only from 0.3.0, opengwasdb ADR "
            "0106). Rewrite it to 0.3.0 and publish that first (`pixi run publish-rewrite`, ADR 0042). "
            "Nothing was written."
        )
    validator = a.bundle.validation.get("validator") if isinstance(a.bundle.validation, Mapping) else None
    if not isinstance(validator, Mapping) or validator.get("name") != register.VALIDATOR_NAME:
        raise PostAdditionRefused(
            f"{a.bundle.root / 'validation.yaml'} is not a Validation Record `register` wrote, so it "
            "describes no published Store to add to. Nothing was written."
        )
    try:
        runtime.verify_runtime_for_releases(
            [(root, store_id, step.name) for step in a.steps], registry_root=registry_root, lock=lock
        )
    except runtime.RuntimeGuardError as exc:
        raise PostAdditionRefused(f"the runtime that would add them is refused: {exc}") from exc
    if probe:
        refuse_without_reflink(paths.store_dir(store_id, root=root))
    return published


def _engines(
    previous: Any, validate_name: str, added: list[tuple[str, Mapping[str, Any]]]
) -> list[dict[str, Any]]:
    """`build_environment.engines`: the published release's, the steps this run reran dropped, then this run's."""
    rerun = {name for name, _ in added} | {validate_name}
    merged: dict[tuple[str, str], list[str]] = {}
    for entry in previous if isinstance(previous, list) else []:
        if not isinstance(entry, Mapping):
            continue
        steps = [s for s in entry.get("steps") or [] if s not in rerun]
        if steps:
            key = (str(entry.get("opengwasdb_commit")), str(entry.get("opengwasdb_version")))
            merged.setdefault(key, []).extend(steps)
    for entry in register.engines_of(added):
        key = (entry["opengwasdb_commit"], entry["opengwasdb_version"])
        merged.setdefault(key, []).extend(s for s in entry["steps"] if s not in merged.get(key, []))
    return [
        {"opengwasdb_commit": commit, "opengwasdb_version": version, "steps": steps}
        for (commit, version), steps in merged.items()
    ]


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _observed(
    a: Assessment,
    previous: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
    replaced: Mapping[str, Mapping[str, Any]],
    *,
    format_version: str,
    store_bytes: int,
    status: str,
) -> dict[str, Any]:
    """The published release's `observed`, with only what this run measured changed."""
    observed = dict(previous) if isinstance(previous, Mapping) else {}
    for key in register.OBSERVED_FIELDS:
        observed.setdefault(key, None)
    observed["format_version"] = format_version
    observed["store_bytes"] = store_bytes
    observed["validate_status"] = status
    added = {base_name(step.name) for step in a.missing}
    facts = {**register.store_facts(a.phase_steps, records, None), **register.clump_facts(a.phase_steps, records)}
    if "rho" in added:
        observed["has_rho"] = facts["has_rho"]
    if "clump" in added:
        for key in ("has_clumps", *register.CLUMP_COUNT_FIELDS):
            observed[key] = facts[key]
    before = _number(observed.get("build_elapsed_s"))
    if before is not None:
        ran = sum(float(records[s.name].get("elapsed_seconds") or 0.0) for s in a.steps)
        rerun = sum(_number(record.get("elapsed_seconds")) or 0.0 for record in replaced.values())
        observed["build_elapsed_s"] = round(before + ran - rerun, 3)
    return observed


def _judge_rewrite_warnings(a: Assessment, validate_record: Mapping[str, Any]) -> None:
    """A rewritten Store's warnings are held to ADR 0042's deviation rule (`publish_rewrite.judge_findings`).

    A `--convert-legacy` release may warn only under a deviation its
    conversion records, and a plain 0.2.3 rewrite not at all: a native
    warning refuses the republication, as it refused the publication.
    """
    rewrite = a.register_record.get("rewrite")
    if not isinstance(rewrite, Mapping):
        return
    payload = json.loads(str(validate_record.get("stdout")))
    findings = payload.get(register.VALIDATE_FINDINGS_KEY)
    if payload.get("warnings") and findings is None:
        raise PostAdditionRefused(
            "the validator printed warnings without `findings`, so none can be told to be a recorded deviation"
        )
    carried = rewrite.get("carried_warnings")
    _deviations, _carried, unrecorded = publish_rewrite.judge_findings(
        findings or [],
        convert_legacy=bool(rewrite.get("convert_legacy")),
        replaced_warnings=[w for w in carried if isinstance(w, str)] if isinstance(carried, list) else [],
    )
    if unrecorded:
        shown = "\n  ".join(f"{f['rule']}: {f['message']}" for f in unrecorded[:10])
        raise PostAdditionRefused(
            f"the staged copy has {len(unrecorded)} warning(s) that are not a deviation its rewrite "
            f"records (ADR 0042):\n  {shown}"
        )


def _failure_detail(record: Mapping[str, Any]) -> str:
    """A failed step's errors: `validate`'s from its JSON verdict, any other's from the end of its stderr."""
    try:
        payload = run.first_json_object(str(record.get("stdout") or ""))
    except ValueError:
        payload = {}
    errors = [str(error) for error in payload.get("errors") or []]
    if errors:
        shown = "\n  ".join(errors[:10])
        more = f"\n  ... and {len(errors) - 10} more" if len(errors) > 10 else ""
        return f"{len(errors)} validation error(s):\n  {shown}{more}"
    return str(record.get("stderr") or "").strip()[-2000:] or "(it printed nothing on stderr)"


class _Run:
    """One call: the records it has run, and the step that failed."""

    def __init__(self, a: Assessment) -> None:
        self.a = a
        self.records: dict[str, dict[str, Any]] = {}
        self.replaced: dict[str, dict[str, Any]] = {}
        self.failed: Path | None = None

    def step(self, step: Step) -> None:
        """Run one step on the staged copy; refuse it unless it succeeded as planned."""
        a = self.a
        run.execute_step(step, a.store_id, artifact_root=a.root, check=False)
        record_p = paths.record_path(a.store_id, step.name, root=a.root)
        record = _record(record_p)
        if not _succeeded(record):
            self.failed = record_p
            raise PostAdditionRefused(
                f"`{' '.join(map(str, (record or {}).get('argv') or step.argv))}` exited "
                f"{(record or {}).get('exit_code')}:\n{_failure_detail(record or {})}"
            )
        register.check_argv_drift(step, record, a.store_id, artifact_root=a.root)  # type: ignore[arg-type]
        self.records[step.name] = record  # type: ignore[assignment]


def _assemble(a: Assessment, r: _Run, stamp: str, snapshot: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The new Validation Record and `publication.json`, through the shared envelope."""
    store_id, root = a.store_id, a.root
    partial = paths.partial_store_path(store_id, root=root)
    records = {
        step.name: record
        for step in a.phase_steps
        if (record := r.records.get(step.name) or _record(paths.record_path(store_id, step.name, root=root)))
    }
    validate_record = records[a.validate.name]
    status, warnings, _errors = register.validate_verdict(validate_record)
    _judge_rewrite_warnings(a, validate_record)
    try:
        staged_info = store_info(partial)
    except (OSError, ValueError) as exc:
        raise PostAdditionRefused(f"`opengwasdb info` cannot read the staged copy {partial}: {exc}") from exc
    previous = dict(a.bundle.validation)
    wrote_store = register.recorded_engine(a.register_record)
    mismatch = register.resolver_mismatch(register.acceptance_evidence(previous), wrote_store)
    if mismatch is not None:
        warnings = [*warnings, mismatch]
        status = "passed_with_warnings"
    now = run.utc_timestamp()
    ran = [step.name for step in a.steps]
    addition = {
        "added_at": now,
        "steps": [step.name for step in a.missing],
        "records": [str(paths.record_path(store_id, name, root=root)) for name in ran],
        "release_id": staged_info.get("release_id"),
        "replaced_archive": str(paths.replaced_dir(store_id, stamp, root=root)),
    }
    # The records that made the Store stay; this run's steps, its `validate` last, follow them.
    inputs = [str(p) for p in a.register_record.get("inputs") or [] if Path(str(p)).stem not in ran]
    inputs += addition["records"]
    return staged_publication.republication(
        b=a.bundle,
        root=root,
        phase=a.phase,
        stamp=stamp,
        snapshot=snapshot,
        now=now,
        status=status,
        warnings=list(warnings),
        validator=register.recorded_engine(validate_record),
        wrote_store=wrote_store,
        engines=_engines(
            (previous.get("build_environment") or {}).get("engines"),
            a.validate.name,
            [(name, records[name]) for name in ran],
        ),
        observed=_observed(
            a,
            previous.get("observed") or {},
            records,
            r.replaced,
            format_version=str(staged_info.get("format_version")),
            store_bytes=store_size_on_disk(partial),
            status=status,
        ),
        inputs=inputs,
        blocks={BLOCK: [*(previous.get(BLOCK) or []), addition]},
        carried_validation=previous,
        carried_register={k: v for k, v in a.register_record.items() if k != "format_version_unread"},
    )


def _terminate(signum: int, _frame: object) -> None:
    """SIGTERM unwinds like Ctrl-C, so a stopped run removes the staged copy and puts records/ back."""
    raise KeyboardInterrupt(f"signal {signum}")


def add_post_steps(
    a: Assessment,
    *,
    registry_root: Path,
    lock: Path | None = None,
    finish: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    """Run `a.steps` on a reflink copy of the published Store and publish it; None when it is up to date.

    The caller holds the release lock and has finished any pending publication.
    Raises `PostAdditionRefused`, having left the published Store, `records/`
    and `validation.yaml` as they were, and `staged_publication.Interrupted`,
    naming the state an interruption left. `finish` carries out the written
    marker and removes it last (`staged_publication.Staging.finish`): the
    entry point's also clears Snakemake's metadata for the phase's records.
    """
    if a.up_to_date:
        return None
    store_id, root = a.store_id, a.root
    preflight(a, registry_root=registry_root, lock=lock)
    stamp = register.unused_archive_stamp(store_id, root)
    store_p, partial = paths.store_path(store_id, root=root), paths.partial_store_path(store_id, root=root)
    r = _Run(a)
    result: dict[str, Any] = {}

    def stage() -> None:
        copied = _reflink(store_p, partial)
        if copied.returncode != 0:
            raise PostAdditionRefused(
                f"`{' '.join(REFLINK_COPY)} {store_p} {partial}` failed on a "
                f"{filesystem_type(store_p.parent)} filesystem: {copied.stderr.strip()}"
            )
        run.fsync_dir(partial.parent)

    def unstage() -> None:
        if os.path.islink(partial):
            partial.unlink()
        elif os.path.lexists(partial):
            shutil.rmtree(partial)
        else:
            return
        run.fsync_dir(partial.parent)

    def work(snapshot: Path) -> Mapping[str, Any]:
        for step in a.steps:
            r.step(step)
        validation, marker = _assemble(a, r, stamp, snapshot)
        result["validation"] = validation
        return marker

    def refuse(exc: register.RegisterError, notes: str) -> PostAdditionRefused:
        return PostAdditionRefused(
            f"Refused to add {', '.join(s.name for s in a.missing) or 'nothing'} to {store_id} "
            f"(running {', '.join(s.name for s in a.steps)}): {exc}\nNothing was published: the published "
            f"Store, records/ and validation.yaml are as they were, and the staged copy is removed.{notes}"
        )

    # The records this run replaces are in the snapshot; any left after it are this call's.
    for step in a.steps:
        record = _record(paths.record_path(store_id, step.name, root=root))
        if record is not None:
            r.replaced[step.name] = record
    previous_term = signal.signal(signal.SIGTERM, _terminate)
    try:
        staged_publication.publish(staged_publication.Staging(
            store_id=store_id,
            root=root,
            stamp=stamp,
            stage=stage,
            unstage=unstage,
            work=work,
            refuse=refuse,
            evidence=lambda: [] if r.failed is None else [staged_publication.Evidence(
                r.failed,
                paths.work_dir(store_id, root=root) / f"post-additions-refused-{stamp}-{r.failed.name}",
                "failed step's record",
            )],
            clear=tuple(paths.record_path(store_id, step.name, root=root) for step in a.steps),
            boundaries=(SNAPSHOT_BOUNDARY, STAGED_BOUNDARY),
            finish=finish,
        ))
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    return result["validation"]


def iter_assessments(store_ids: list[str], registry_root: Path, root: Path) -> Iterator[tuple[str, Assessment]]:
    """Each named release this mode takes, with its assessment."""
    for store_id in store_ids:
        if not paths.is_valid_store_id(store_id) or not (registry_root / store_id / "release.yaml").is_file():
            continue
        assessment = assess(bundle.load(store_id, registry_root=registry_root), root)
        if assessment is not None:
            yield store_id, assessment


__all__ = [
    "ADDABLE_STEPS",
    "Assessment",
    "BLOCK",
    "OVERVIEW_STEP",
    "PostAdditionRefused",
    "REFLINK_COPY",
    "SNAPSHOT_BOUNDARY",
    "STAGED_BOUNDARY",
    "add_post_steps",
    "assess",
    "current",
    "filesystem_type",
    "is_addable",
    "iter_assessments",
    "preflight",
    "refuse_without_reflink",
]
