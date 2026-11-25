"""Publish a rewritten Store Release through the registry (#533, ADR 0042).

`opengwasdb rewrite-release` derives a new format-0.3.0 Store Release from a
published 0.2.x one and stages it beside it, `<id>/store.opengwasdb.0.3.0`,
leaving the published release untouched. A Dense release before 0.2.3 is
converted (`--convert-legacy`, opengwas/opengwasdb#492): what a rebuild alone
can fix is recorded in its manifest's `provenance.converted_from` as validator
rule names, and `validate` reports exactly those rules as warnings. Owner
rulings require each rewrite to be published under the registry's rules,
replacing the 0.2.x release (#499, 24 Nov 2026; #520, 25 Nov 2026).

    pixi run publish-rewrite OGS-00015 \\
        --staged /data/opengwasdb/stores/OGS-00015/store.opengwasdb.0.3.0 \\
        --evidence <run>/logs/convert/OGS-00015

One call, under the release's run lock:

1. finishes a publication of a rewrite that a crash left
   (`register.complete_publication`), and does nothing when this staged
   release is already the published one;
2. refuses, before writing anything: what a killed run or an unfinished
   completion left beside the Store, a staged release that is missing, not
   0.3.0 or not this Store, evidence that does not show the rewrite of this
   published Store to this staged path exiting 0, a published Store or engine
   install that changed after the rewrite started, and a runtime that is not
   the locked engine (`runtime.verify_runtime_for_releases`);
3. copies `records/` to a forced run's snapshot (`run.snapshot_records`),
   moves the staged release to `store.opengwasdb.partial`, records the rewrite
   as the `rewrite-release` step from the operator's evidence, and runs the
   recipe's planned `validate` step on it (`run.execute_step`). The rewrite
   validated its release too, but prints only errors and keeps no verdict;
4. refuses any validation error, and any warning that is not a deviation the
   conversion records: a warning is accepted only from a `--convert-legacy`
   rewrite and only under one of the three rules a conversion records. On a
   0.3.0 release each is an error unless the release records it
   (opengwasdb's `Rule.finding`), so a warning under one is the record's;
5. writes `publication.json` (`register.begin_publication`) and hands it to
   `register.complete_publication`, so the old Store is archived under
   `replaced/<UTC>/` with its records and Validation Record.

Until the marker exists, any failure (a refusal, an error, Ctrl-C, SIGTERM)
puts `records/` and the staged release back as they were; from then on,
recovery is the marker's. The registry reads the Stores only through
`opengwasdb info --format json` and `validate --format json` (ADR 0023):
`provenance.converted_from` is not read, the validator's findings are the
record of it.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ogstores import bundle, engine, paths, register, run, runtime
from ogstores.plan import BUILD_PHASE, COMPLETION_PHASE, REGISTER_STEPS, Step, plan
from ogstores.runtime import REQUIRED_FORMAT_VERSION
from ogstores.store_info import store_info
from ogstores.store_size import store_size_on_disk

#: `opengwasdb rewrite-release`: the command the evidence must show, and the step it is recorded as.
REWRITE_STEP: str = run.REWRITE_RELEASE_STEP
CONVERT_LEGACY_FLAG: str = "--convert-legacy"

#: The rules a `--convert-legacy` conversion can record as deviations, the only
#: rules `validate` lowers to warnings for one (opengwasdb
#: `legacy_conversion.DEVIATION_RULES`, #492). A rule the engine adds later is
#: refused here until it is listed: the safe direction for a copy to drift.
DEVIATION_RULES: frozenset[str] = frozenset({
    "variant_table.indel_order",
    "variant_table.source_alid",
    "side_tables.se_exceptions_rounded",
})

#: A release `rewrite-release` reads is of the 0.2 series (0.2.3, or 0.2.0-0.2.2 converted).
SOURCE_FORMAT_SERIES: str = "0.2."

#: The files of the operator's evidence, `<prefix>.<suffix>`, as #525's queue script writes them.
EVIDENCE_SUFFIXES: tuple[str, ...] = ("cmd", "log", "time", "start", "end", "exit")

# `<command> (budget <N>s)`: the queue script notes its `timeout` budget after the command.
_BUDGET = re.compile(r"\A(?P<command>.*?)(?: \(budget (?P<budget>\d+)s\))?\Z", re.DOTALL)
_TIMED = re.compile(r'^\s*Command being timed: "(?P<command>.*)"\s*$', re.MULTILINE)
_PEAK_RSS = re.compile(r"^\s*Maximum resident set size \(kbytes\): (?P<kb>\d+)\s*$", re.MULTILINE)
_TIME_EXIT = re.compile(r"^\s*Exit status: (?P<code>-?\d+)\s*$", re.MULTILINE)

#: How many errors or warnings a refusal names.
_SHOWN = 10


class RewritePublicationRefused(register.RegisterError):
    """A rewritten release this route will not publish; the published release is as it was."""


@dataclass(frozen=True)
class RewriteEvidence:
    """What the operator's evidence says the rewrite was: one run of `rewrite-release`."""

    prefix: Path
    argv: list[str]
    budget_seconds: int | None
    start: datetime.datetime
    end: datetime.datetime
    start_time: str
    end_time: str
    exit_code: int
    peak_rss_kb: int
    log: str

    @property
    def executable(self) -> str:
        return self.argv[0]

    @property
    def source(self) -> Path:
        return Path(self.argv[2])

    @property
    def destination(self) -> Path:
        return Path(self.argv[3])

    @property
    def convert_legacy(self) -> bool:
        return CONVERT_LEGACY_FLAG in self.argv[4:]

    @property
    def elapsed_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def path(self, suffix: str) -> Path:
        return self.prefix.with_name(f"{self.prefix.name}.{suffix}")


def _utc(text: str, where: Path) -> datetime.datetime:
    try:
        value = datetime.datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise RewritePublicationRefused(f"{where} holds {text.strip()!r}, not a UTC time") from exc
    if value.tzinfo is None:
        raise RewritePublicationRefused(f"{where} holds {text.strip()!r}, with no time zone")
    return value


def read_evidence(prefix: Path | str) -> RewriteEvidence:
    """Read the operator's evidence of one `rewrite-release` run, refusing any it cannot trust.

    `<prefix>.cmd` is the command, optionally followed by ` (budget <N>s)`;
    `.log` its stdout and stderr; `.time` its `/usr/bin/time -v` report, whose
    timed command must end with exactly that command and whose exit status must
    be `.exit`'s; `.start` and `.end` its UTC start and end. Every file is read
    as UTF-8 exactly: evidence is not repaired.
    """
    prefix = Path(prefix)

    def text(suffix: str) -> str:
        path = prefix.with_name(f"{prefix.name}.{suffix}")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RewritePublicationRefused(f"the rewrite's evidence {path} cannot be read: {exc}") from exc

    texts = {suffix: text(suffix) for suffix in EVIDENCE_SUFFIXES}
    cmd_p, time_p = prefix.with_name(f"{prefix.name}.cmd"), prefix.with_name(f"{prefix.name}.time")
    match = _BUDGET.match(texts["cmd"].strip())
    assert match is not None  # the pattern matches any text
    command = match.group("command")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RewritePublicationRefused(f"{cmd_p} is not one shell command: {exc}: {command!r}") from exc
    if len(argv) < 4 or argv[1] != REWRITE_STEP:
        raise RewritePublicationRefused(
            f"{cmd_p} is {command!r}, not `<opengwasdb> {REWRITE_STEP} SOURCE DESTINATION ...`"
        )
    timed = _TIMED.findall(texts["time"])
    if len(timed) != 1 or not (timed[0] == command or timed[0].endswith(" " + command)):
        raise RewritePublicationRefused(
            f"{time_p} does not time {cmd_p}'s command: its `Command being timed` is "
            f"{timed!r}, which does not end with {command!r}"
        )
    rss = _PEAK_RSS.findall(texts["time"])
    time_exit = _TIME_EXIT.findall(texts["time"])
    if len(rss) != 1 or len(time_exit) != 1:
        raise RewritePublicationRefused(
            f"{time_p} is not one `/usr/bin/time -v` report: it needs one `Maximum resident set "
            "size (kbytes)` and one `Exit status` line"
        )
    exit_text = texts["exit"].strip()
    if not re.fullmatch(r"-?\d+", exit_text):
        raise RewritePublicationRefused(f"{prefix}.exit holds {exit_text!r}, not an exit code")
    exit_code = int(exit_text)
    if int(time_exit[0]) != exit_code:
        raise RewritePublicationRefused(
            f"{prefix}.exit says {exit_code} but {time_p} says the command exited {time_exit[0]}"
        )
    start = _utc(texts["start"], prefix.with_name(f"{prefix.name}.start"))
    end = _utc(texts["end"], prefix.with_name(f"{prefix.name}.end"))
    if end < start:
        raise RewritePublicationRefused(f"{prefix}.end is before {prefix}.start")
    return RewriteEvidence(
        prefix=prefix,
        argv=argv,
        budget_seconds=int(match.group("budget")) if match.group("budget") else None,
        start=start,
        end=end,
        start_time=texts["start"].strip(),
        end_time=texts["end"].strip(),
        exit_code=exit_code,
        peak_rss_kb=int(rss[0]),
        log=texts["log"],
    )


def _info(path: Path, what: str) -> dict[str, Any]:
    try:
        return store_info(path)
    except (OSError, ValueError) as exc:
        raise RewritePublicationRefused(f"`opengwasdb info` cannot read the {what} at {path}: {exc}") from exc


def _field(info: Mapping[str, Any], key: str, what: str) -> str:
    """A non-empty string `info` reported; an absent one is refused, never compared as equal."""
    value = info.get(key)
    if not isinstance(value, str) or not value:
        raise RewritePublicationRefused(f"`opengwasdb info` reports no {key} for the {what}")
    return value


def _record(path: Path) -> dict[str, Any] | None:
    try:
        return run.read_record(path)
    except ValueError as exc:
        raise RewritePublicationRefused(str(exc)) from exc


def _publishing_phase(store_id: str, root: Path) -> str:
    """The phase whose register record describes the published Store (ADR 0037).

    A release that completed its own Store holds `records/completion-register.json`;
    the rewrite replaces what that phase published, so it writes that record and
    leaves the build phase's as it is.
    """
    completion = paths.record_path(store_id, REGISTER_STEPS[COMPLETION_PHASE], root=root)
    return COMPLETION_PHASE if os.path.lexists(completion) else BUILD_PHASE


def _finish_own_publication(store_id: str, root: Path) -> None:
    """Finish a publication of a rewrite that a crash left; refuse any other pending one.

    A marker a register job left is the entry point's to finish: it also clears
    Snakemake's mark that the killed job's record is incomplete
    (`release.finish_publication`), which this command cannot.
    """
    marker = register.load_publication_marker(store_id, root)
    if marker is None:
        return
    record = marker.get("register_record")
    if not (isinstance(record, Mapping) and isinstance(record.get("rewrite"), Mapping)):
        raise RewritePublicationRefused(
            f"{paths.publication_marker(store_id, root=root)} is a pending publication of another "
            f"run; `pixi run release {store_id}` finishes it first"
        )
    register.complete_publication(store_id, root)
    print(f"publish-rewrite: finished the interrupted publication of {store_id}")


def _already_published(store_id: str, staged: Path, root: Path) -> bool:
    """Whether this staged release was published by an earlier call and is the published Store."""
    if os.path.lexists(staged):
        return False
    record = _record(paths.record_path(store_id, REGISTER_STEPS[_publishing_phase(store_id, root)], root=root))
    rewrite = (record or {}).get("rewrite")
    if not isinstance(rewrite, Mapping) or rewrite.get("staged") != str(staged):
        return False
    published = _info(paths.store_path(store_id, root=root), "published Store")
    return _field(published, "release_id", "published Store") == rewrite.get("release_id")


def _refuse_leftovers(store_id: str, staged: Path, root: Path) -> None:
    """Refuse what a killed run or an unfinished completion left beside the Store, naming the remedy.

    A snapshot is reported first, with the `.partial` release a killed
    publication of a rewrite leaves beside it, so neither hides the other.
    """
    partial = paths.partial_store_path(store_id, root=root)
    snapshots = run.pending_force_snapshots(store_id, root)
    if snapshots:
        moved = (
            f" It also left the staged release at {partial}: move it back to {staged} after that."
            if os.path.lexists(partial) else ""
        )
        raise RewritePublicationRefused(
            f"{store_id} holds a forced run's records snapshot, {snapshots[0]}, that no publication "
            f"finished: a killed run left it. Put records/ back with `pixi run release {store_id} "
            "--resolve-snapshot restore` (or `delete` it when records/ describes the published "
            f"Store).{moved}"
        )
    for leftover, what in (
        (partial, "a staged release no run is publishing"),
        (paths.backup_store_path(store_id, root=root), "a replaced Store no publication finished"),
        (paths.completion_checkpoint_path(store_id, root=root), "an unfinished completion's checkpoint"),
        (paths.completion_start_path(store_id, root=root), "an unfinished completion's start record"),
    ):
        if os.path.lexists(leftover):
            raise RewritePublicationRefused(
                f"{leftover} exists: {what}. A rewrite published now would replace the Store it "
                f"belongs to, so finish or discard it first (`pixi run release {store_id}`)"
            )


def _refuse_unpublishable(
    b: bundle.Bundle, staged: Path, evidence: RewriteEvidence, root: Path, register_record_p: Path
) -> tuple[dict[str, Any], dict[str, Any], engine.EngineIdentity]:
    """Everything checked before anything is written; returns both releases' `info` and the engine."""
    store_id = b.store_id
    store_dir = paths.store_dir(store_id, root=root)
    store_p = paths.store_path(store_id, root=root)
    if staged.parent != store_dir or staged.name in {
        store_p.name,
        paths.partial_store_path(store_id, root=root).name,
        paths.backup_store_path(store_id, root=root).name,
    }:
        raise RewritePublicationRefused(
            f"{staged} is not a staged release of {store_id}: one is a directory of its own "
            f"beside the published Store, in {store_dir}, so publishing it is a rename"
        )
    if not store_p.is_dir():
        raise RewritePublicationRefused(f"{store_id} has no published Store at {store_p} to replace")
    if evidence.exit_code != 0:
        raise RewritePublicationRefused(
            f"the rewrite exited {evidence.exit_code} ({evidence.path('exit')}); only a rewrite "
            "that exited 0 published a release"
        )
    if evidence.source.resolve() != store_p.resolve() or evidence.destination.resolve() != staged.resolve():
        raise RewritePublicationRefused(
            f"the rewrite ({evidence.path('cmd')}) read {evidence.source} and wrote {evidence.destination}, "
            f"not the published Store {store_p} and the staged release {staged}"
        )
    # A path is not a release: the Store the rewrite read must still be the one there.
    started = evidence.start.timestamp()
    for changed, what in ((store_p, "published Store"), (register_record_p, "register record")):
        if changed.stat().st_mtime > started:
            raise RewritePublicationRefused(
                f"the {what} {changed} changed after the rewrite started ({evidence.start_time}), "
                "so the release it read may not be the one published now"
            )
    identity = engine.identify(evidence.executable)
    if not engine.is_exact_commit_hash(identity.commit):
        raise RewritePublicationRefused(
            f"the rewrite's engine {evidence.executable} names no exact commit in its install, so "
            "the record could not say which engine wrote the release"
        )
    # `info` does not name the engine that wrote a release, so the install must
    # be the one that ran: not reinstalled since the rewrite started.
    install = engine.executable_install_record(evidence.executable)
    if install is None or install.stat().st_mtime > started:
        raise RewritePublicationRefused(
            f"the rewrite's engine install ({install or evidence.executable}) was written after the "
            f"rewrite started ({evidence.start_time}), so its commit {identity.commit} may not be "
            "the engine that ran"
        )
    # Each `info` starts the engine (about 2 s), so the two are asked side by side.
    with ThreadPoolExecutor(max_workers=2) as pool:
        published, candidate = pool.map(_info, (store_p, staged), ("published Store", "staged release"))
    candidate_format = _field(candidate, "format_version", "staged release")
    if candidate_format != REQUIRED_FORMAT_VERSION:
        raise RewritePublicationRefused(
            f"the staged release {staged} is format {candidate_format!r}; only a "
            f"{REQUIRED_FORMAT_VERSION} release is published over a rewritten one"
        )
    published_format = _field(published, "format_version", "published Store")
    if not published_format.startswith(SOURCE_FORMAT_SERIES):
        raise RewritePublicationRefused(
            f"the published Store {store_p} is format {published_format!r}, which "
            f"`{REWRITE_STEP}` does not read: it rewrites a {SOURCE_FORMAT_SERIES}x release"
        )
    for what, info in (("staged release", candidate), ("published Store", published)):
        if _field(info, "store_id", what) != store_id:
            raise RewritePublicationRefused(
                f"the {what} names Store {info['store_id']!r}, not {store_id}; a rewrite keeps its source's"
            )
    if _field(candidate, "release_id", "staged release") == _field(published, "release_id", "published Store"):
        raise RewritePublicationRefused(
            f"the staged release's release_id is {candidate['release_id']!r}, the published "
            "Store's; a rewrite is a new release"
        )
    return published, candidate, identity


def _validate_step(b: bundle.Bundle, phase: str, root: Path) -> Step:
    """The recipe's planned `validate` step of the publishing phase, with its declared workers."""
    phase_steps = [step for step in plan(b, artifact_root=root) if step.phase == phase]
    name = register.phase_validate_step(phase_steps)
    for step in phase_steps:
        if step.name == name:
            return step
    raise RewritePublicationRefused(
        f"{b.store_id}'s Build Recipe plans no {name!r} step in its {phase} phase, so there is "
        "no verdict to publish the rewrite on"
    )


def judge_findings(
    findings: list[Mapping[str, str]], *, convert_legacy: bool
) -> tuple[list[str], list[Mapping[str, str]]]:
    """The deviations the warnings are, and the warnings that are none.

    A warning is a recorded deviation only on a `--convert-legacy` rewrite and
    only under one of `DEVIATION_RULES`; a 0.2.3 rewrite records none (#499).
    """
    accepted: set[str] = set()
    unrecorded: list[Mapping[str, str]] = []
    for item in findings:
        if item["severity"] != "warning":
            continue
        if convert_legacy and item["rule"] in DEVIATION_RULES:
            accepted.add(item["rule"])
        else:
            unrecorded.append(item)
    return sorted(accepted), unrecorded


def _listed(items: list[str]) -> str:
    shown = "\n  ".join(items[:_SHOWN])
    more = f"\n  ... and {len(items) - _SHOWN} more" if len(items) > _SHOWN else ""
    return f"\n  {shown}{more}"


def _refuse_failed_validation(record: Mapping[str, Any] | None) -> None:
    """Refuse a validate step that did not exit 0, naming its errors.

    A failed step's output may be captured twice (`run.execute_step` drains the
    process group), so its verdict is the first JSON object on stdout, as the
    Observed-View Check's is read; `register.validate_verdict` reads only a
    passing one exactly.
    """
    if record is None:
        raise RewritePublicationRefused("the validate step left no record")
    if record.get("exit_code") == 0 and record.get("success") is True:
        return
    try:
        payload = run.first_json_object(str(record.get("stdout") or ""))
    except ValueError:
        payload = {}
    findings = payload.get(register.VALIDATE_FINDINGS_KEY) or []
    errors = [
        f"{item.get('rule')}: {item.get('message')}"
        for item in findings
        if isinstance(item, Mapping) and item.get("severity") == "error"
    ] or [str(error) for error in payload.get("errors") or []]
    if not errors:
        errors = [str(record.get("stderr") or "").strip()[-2000:] or "(it printed no verdict)"]
    raise RewritePublicationRefused(
        f"the staged release fails validation: `{' '.join(map(str, record.get('argv') or []))}` "
        f"exited {record.get('exit_code')} with {len(errors)} error(s):" + _listed(errors)
    )


def _judge_validation(record: Mapping[str, Any] | None, *, convert_legacy: bool) -> tuple[str, list[str], list[str]]:
    """This run's verdict and warnings, and the deviations they are; refuses anything else."""
    _refuse_failed_validation(record)
    status, warnings, _errors = register.validate_verdict(dict(record or {}))
    findings = json.loads(str((record or {}).get("stdout"))).get(register.VALIDATE_FINDINGS_KEY)
    if warnings and findings is None:
        raise RewritePublicationRefused(
            "the validator printed warnings without `findings`, so no warning can be told to "
            "be a recorded deviation"
        )
    deviations, unrecorded = judge_findings(findings or [], convert_legacy=convert_legacy)
    if unrecorded:
        accepts = ", ".join(sorted(DEVIATION_RULES)) if convert_legacy else "none"
        what = "a --convert-legacy conversion's" if convert_legacy else "a 0.2.3 rewrite's"
        raise RewritePublicationRefused(
            f"the staged release has {len(unrecorded)} warning(s) that are not a deviation "
            f"{what} record accepts ({accepts}):"
            + _listed([f"{f['rule']}: {f['message']}" for f in unrecorded])
        )
    return status, warnings, deviations


def _observed(
    previous: Mapping[str, Any] | None,
    *,
    format_version: str,
    store_bytes: int,
    status: str,
    convert_legacy: bool,
    elapsed: list[float],
) -> dict[str, Any]:
    """The rewritten release's `observed` block (ADR 0042).

    The rewrite answers every question its source does (the engine's digest
    proof refuses it otherwise), so what the build reported about the data is
    carried from the replaced Validation Record. Measured anew: the format
    (`info`), the size on disk (a stat) and the verdict. `build_elapsed_s` is
    the schema's sum of step times: the replaced release's, the rewrite's and
    this validation's, or null when the replaced one is unknown. A conversion
    masks every association whose SE is above 65,504, so the counts the build
    reported no longer describe it and nothing reports the new ones: `null`.
    """
    carried = dict(previous) if isinstance(previous, Mapping) else {}
    observed: dict[str, Any] = {field: carried.get(field) for field in register.OBSERVED_FIELDS}
    observed.update({key: value for key, value in carried.items() if key not in observed})
    observed["format_version"] = format_version
    observed["store_bytes"] = store_bytes
    observed["validate_status"] = status
    before = carried.get("build_elapsed_s")
    observed["build_elapsed_s"] = (
        round(float(before) + sum(elapsed), 3) if isinstance(before, (int, float)) and not isinstance(before, bool) else None
    )
    if convert_legacy:
        observed["n_associations"] = None
        observed["n_top_hits"] = None
    return observed


def _rewrite_record(
    store_id: str, root: Path, evidence: RewriteEvidence, identity: engine.EngineIdentity, staged: Path
) -> dict[str, Any]:
    """The `rewrite-release` step record: a `run.StepResult` from the evidence, and what the evidence adds."""
    result = run.StepResult(
        step=REWRITE_STEP,
        store_id=store_id,
        exit_code=evidence.exit_code,
        success=evidence.exit_code == 0,
        start_time=evidence.start_time,
        end_time=evidence.end_time,
        elapsed_seconds=evidence.elapsed_seconds,
        argv=list(evidence.argv),
        planned_argv=list(evidence.argv),
        inputs=[str(evidence.source)],
        outputs=[str(staged)],
        opengwasdb_rev=identity.commit,
        opengwasdb_version=identity.version,
        opengwasdb_executable=identity.executable,
        stdout=evidence.log,
        stderr="",
        record_path=str(paths.record_path(store_id, REWRITE_STEP, root=root)),
    )
    return {
        **result.to_dict(),
        "peak_rss_kb": evidence.peak_rss_kb,
        "budget_seconds": evidence.budget_seconds,
        "log_path": str(evidence.path("log")),
        "evidence": {suffix: str(evidence.path(suffix)) for suffix in EVIDENCE_SUFFIXES},
    }


@dataclass(frozen=True)
class _Publication:
    """What one call publishes, verified before any write."""

    bundle: bundle.Bundle
    root: Path
    staged: Path
    evidence: RewriteEvidence
    identity: engine.EngineIdentity
    published: dict[str, Any]
    candidate: dict[str, Any]
    phase: str
    replaced_register: dict[str, Any]
    validate_step: Step
    stamp: str


def _assemble(
    p: _Publication,
    validate_record: Mapping[str, Any],
    status: str,
    warnings: list[str],
    deviations: list[str],
    snapshot: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The new Validation Record and `publication.json`, through `register`'s builders."""
    store_id, root = p.bundle.store_id, p.root
    now = run.utc_timestamp()
    archive = paths.replaced_dir(store_id, p.stamp, root=root)
    rewrite_p = paths.record_path(store_id, REWRITE_STEP, root=root)
    rewrite_record = run.read_record(rewrite_p) or {}
    previous = p.bundle.validation if isinstance(p.bundle.validation, Mapping) else {}
    rewrite = {
        "record": str(rewrite_p),
        "staged": str(p.staged),
        "command": list(p.evidence.argv),
        "convert_legacy": p.evidence.convert_legacy,
        "release_id": p.candidate["release_id"],
        "format_version": p.candidate["format_version"],
        "source_release_id": p.published["release_id"],
        "source_format_version": p.published["format_version"],
        "deviations": list(deviations),
    }
    validation = register.build_validation_record(
        status=status,
        checks_store=status,
        validated_at=now,
        validator=register.recorded_engine(validate_record),
        wrote_store=p.identity,
        engines=register.engines_of(
            [(REWRITE_STEP, rewrite_record), (p.validate_step.name, validate_record)]
        ),
        observed=_observed(
            previous.get("observed"),
            format_version=p.candidate["format_version"],
            store_bytes=store_size_on_disk(paths.partial_store_path(store_id, root=root)),
            status=status,
            convert_legacy=p.evidence.convert_legacy,
            elapsed=[p.evidence.elapsed_seconds, float(validate_record.get("elapsed_seconds") or 0.0)],
        ),
        warnings=list(warnings),
        errors=[],
        acceptance=register.acceptance_evidence(previous),
        replaced={"archive": str(archive), "replaced_at": now},
    )
    validation["rewrite"] = rewrite
    register_step = REGISTER_STEPS[p.phase]
    inputs = [str(path) for path in p.replaced_register["inputs"]]
    if str(rewrite_p) not in inputs:
        inputs.append(str(rewrite_p))
    register_record = register.build_register_record(
        step=register_step,
        store_id=store_id,
        at=now,
        inputs=inputs,
        outputs=[str(p.bundle.root / "validation.yaml")],
        wrote_store=p.identity,
        record_path=paths.record_path(store_id, register_step, root=root),
        published_store=str(paths.store_path(store_id, root=root)),
        replaced_archive=str(archive),
    )
    register_record["rewrite"] = rewrite
    val_yaml = p.bundle.root / "validation.yaml"
    marker = register.build_publication_marker(
        store_id=store_id,
        archive=archive,
        snapshot=snapshot,
        replaced_store=register.ARCHIVE_REPLACED_STORE,
        validation_yaml=val_yaml,
        previous_validation=val_yaml.read_text(encoding="utf-8") if val_yaml.is_file() else None,
        validation_record=validation,
        register_record=register_record,
        written_at=now,
    )
    return validation, marker


def _prepare(
    b: bundle.Bundle, staged: Path, evidence_prefix: Path | str, root: Path, lock: Path | None
) -> _Publication:
    """Every check before the first write (module docstring, step 2)."""
    store_id = b.store_id
    _refuse_leftovers(store_id, staged, root)
    if not staged.is_dir() or staged.is_symlink():
        raise RewritePublicationRefused(f"there is no staged release at {staged}: nothing to publish")
    evidence = read_evidence(evidence_prefix)
    phase = _publishing_phase(store_id, root)
    register_record_p = paths.record_path(store_id, REGISTER_STEPS[phase], root=root)
    replaced_register = _record(register_record_p)
    if replaced_register is None:
        raise RewritePublicationRefused(
            f"{store_id} has no {register_record_p.name} for the release it would replace"
        )
    inputs = replaced_register.get("inputs")
    if not isinstance(inputs, list) or not inputs or not all(isinstance(i, str) for i in inputs):
        raise RewritePublicationRefused(
            f"{register_record_p} names no records it registered (`inputs` is {inputs!r}), so the "
            "rewritten release's lineage could not be kept"
        )
    published, candidate, identity = _refuse_unpublishable(b, staged, evidence, root, register_record_p)
    validate_step = _validate_step(b, phase, root)
    try:
        runtime.verify_runtime_for_releases(
            [(root, store_id, validate_step.name)], registry_root=b.root.parent, lock=lock
        )
    except runtime.RuntimeGuardError as exc:
        raise RewritePublicationRefused(f"the runtime that would validate it is refused: {exc}") from exc
    return _Publication(
        bundle=b,
        root=root,
        staged=staged,
        evidence=evidence,
        identity=identity,
        published=published,
        candidate=candidate,
        phase=phase,
        replaced_register=replaced_register,
        validate_step=validate_step,
        stamp=register.unused_archive_stamp(store_id, root),
    )


def _roll_back(p: _Publication, snapshot: Path | None) -> str:
    """Put `records/` and the staged release back; return what the refusal should add.

    The staged release goes back first. The refused verdict is then copied, byte
    for byte, before `records/` is restored over it: it is evidence, not part of
    the release, so any failure to keep it is only noted, and the restores run
    whatever it raised.
    """
    store_id, root = p.bundle.store_id, p.root
    notes: list[str] = []
    partial = paths.partial_store_path(store_id, root=root)
    try:
        if os.path.lexists(partial) and not os.path.lexists(p.staged):
            partial.rename(p.staged)
            run.fsync_dir(p.staged.parent)
    finally:
        try:
            validate_p = paths.record_path(store_id, p.validate_step.name, root=root)
            if snapshot is not None and validate_p.is_file():
                kept = paths.work_dir(store_id, root=root) / f"publish-rewrite-refused-{p.stamp}-validate.json"
                try:
                    kept.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(validate_p, kept)
                    notes.append(f" The validate record is kept at {kept}.")
                except Exception as exc:  # evidence only: never a reason to stop the rollback
                    notes.append(f" The validate record could not be kept ({exc!r}).")
        finally:
            if snapshot is not None and snapshot.exists():
                run.restore_force_snapshot(snapshot)
    return "".join(notes)


def publish_rewrite(
    store_id: str,
    staged: Path | str,
    evidence_prefix: Path | str,
    *,
    registry_root: Path | str | None = None,
    artifact_root: Path | str | None = None,
    lock: Path | None = None,
) -> dict[str, Any] | None:
    """Publish the rewritten release staged at `staged` over `store_id`'s published Store.

    Returns the Validation Record written, or None when this staged release
    is already the published one. Raises `RewritePublicationRefused`, having
    left the published Store, `records/` and `validation.yaml` as they were
    and the staged release where it was. The caller holds the release lock.
    `lock` is the `pixi.lock` the validating runtime must match (the
    repository's by default).
    """
    paths.require_valid_store_id(store_id)
    root = Path(artifact_root) if artifact_root is not None else paths.artifact_root()
    staged = Path(staged)
    b = bundle.load(store_id, registry_root=registry_root)

    _finish_own_publication(store_id, root)
    if _already_published(store_id, staged, root):
        return None
    p = _prepare(b, staged, evidence_prefix, root, lock)

    marker_p = paths.publication_marker(store_id, root=root)
    partial = paths.partial_store_path(store_id, root=root)
    snapshot: Path | None = None
    try:
        snapshot = run.snapshot_records(store_id, root, p.stamp)
        run.fault_boundary("rewrite-snapshot-taken")
        # The replaced release's verdict is in the snapshot: any validate record
        # from here on is this call's, and a refusal keeps only that one.
        paths.record_path(store_id, p.validate_step.name, root=root).unlink(missing_ok=True)
        staged.rename(partial)
        run.fsync_dir(partial.parent)
        run.fault_boundary("rewrite-staged")
        run.write_record_atomically(
            _rewrite_record(store_id, root, p.evidence, p.identity, staged),
            paths.record_path(store_id, REWRITE_STEP, root=root),
        )
        run.execute_step(p.validate_step, store_id, artifact_root=root, check=False)
        validate_record = run.load_record(store_id, p.validate_step.name, root=root)
        status, warnings, deviations = _judge_validation(
            validate_record, convert_legacy=p.evidence.convert_legacy
        )
        validation, marker = _assemble(p, validate_record or {}, status, warnings, deviations, snapshot)
        register.begin_publication(marker, root)
    except BaseException as exc:
        if os.path.lexists(marker_p):
            raise  # committed: `complete_publication` recovers it from the marker
        notes = _roll_back(p, snapshot)
        if isinstance(exc, register.RegisterError):
            raise RewritePublicationRefused(
                f"Refused to publish {staged} over {store_id}: {exc}\nNothing was published: the "
                "published Store, records/ and validation.yaml are as they were, and the staged "
                f"release is back at its path.{notes}"
            ) from exc
        raise
    register.complete_publication(store_id, root)
    return validation


def _terminate(signum: int, _frame: object) -> None:
    """SIGTERM unwinds like Ctrl-C, so a stopped run puts back what it moved."""
    raise KeyboardInterrupt(f"signal {signum}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publish-rewrite",
        description="Publish a Store Release `opengwasdb rewrite-release` staged beside the published one (#533).",
    )
    parser.add_argument("store_id", help="the registered Store Release, e.g. OGS-00015")
    parser.add_argument("--staged", required=True, type=Path, help="the rewritten release, beside the published Store")
    parser.add_argument(
        "--evidence", required=True, type=Path,
        help="prefix of the rewrite's evidence: <prefix>.cmd, .log, .time, .start, .end, .exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from ogstores import release

    args = parse_args(argv)
    root = paths.artifact_root()
    signal.signal(signal.SIGTERM, _terminate)
    locks = release.ReleaseLocks(root)
    try:
        locks.acquire(args.store_id)
    except release.ReleaseInProgressError as exc:
        print(f"publish-rewrite: {exc}", file=sys.stderr)
        return 1
    try:
        record = publish_rewrite(args.store_id, args.staged, args.evidence, artifact_root=root)
    except register.RegisterError as exc:
        print(f"publish-rewrite: {exc}", file=sys.stderr)
        return 1
    finally:
        locks.release_all()
    if record is None:
        print(f"publish-rewrite: {args.staged} is already {args.store_id}'s published Store; nothing to do")
        return 0
    rewrite = record["rewrite"]
    print(
        f"publish-rewrite: published {rewrite['release_id']} (format {rewrite['format_version']}) as "
        f"{args.store_id}, replacing {rewrite['source_release_id']} (format "
        f"{rewrite['source_format_version']}), archived at {record['replaced']['archive']}; "
        f"validation {record['status']}, deviations {rewrite['deviations'] or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
