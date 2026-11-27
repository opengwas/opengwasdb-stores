"""Publish a Store staged outside a Snakemake run, through the publication transaction (#533, #528).

Two routes replace a published Store with one staged beside it, outside
Snakemake: `ogstores.publish_rewrite` (a rewritten release, ADR 0043) and
`ogstores.post_additions` (the published Store with missing post steps added,
ADR 0044). This module owns what they share, so neither has its own:

- the lifecycle (`publish`): copy `records/` to a forced run's snapshot, stage
  the candidate at `store.opengwasdb.partial`, run the mode's work, write
  `publication.json`, then `register.complete_publication`;
- the rollback: until the marker exists, any failure undoes the staging,
  keeps the mode's evidence in `work/`, and restores `records/` from the
  snapshot, with Ctrl-C and SIGTERM ignored while it runs;
- the transaction state an interruption leaves (`Interrupted.state`);
- the record envelope (`republication`): the new Validation Record, the
  phase's register record and the marker, from `register`'s builders.

What each mode stages, and how it judges its verdict, stay the mode's.
"""

from __future__ import annotations

import os
import shutil
import signal
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ogstores import bundle, engine, paths, register, run
from ogstores.plan import REGISTER_STEPS

#: What an interrupted publication left: nothing (`ROLLED_BACK`), a marker the
#: next run finishes (`PENDING`), or a finished publication (`PUBLISHED`).
ROLLED_BACK: str = "rolled_back"
PENDING: str = "pending"
PUBLISHED: str = "published"


class Interrupted(KeyboardInterrupt):
    """Ctrl-C or SIGTERM during a staged publication, and the state it left."""

    def __init__(self, store_id: str, state: str) -> None:
        super().__init__(f"{store_id}: {state}")
        self.store_id = store_id
        self.state = state

    def describe(self) -> str:
        if self.state == PENDING:
            return (
                f"the publication of {self.store_id} had begun ({paths.PUBLICATION_MARKER} is written): "
                f"the next `pixi run release {self.store_id}` finishes it"
            )
        if self.state == PUBLISHED:
            return f"the publication of {self.store_id} had finished: its new Store and records are published"
        return (
            f"nothing was published: {self.store_id}'s published Store, records/ and validation.yaml "
            "are as they were, and the staged copy is removed"
        )


@dataclass(frozen=True)
class Evidence:
    """A record a refused call keeps in `work/`, copied byte for byte: `what` names it in the refusal."""

    record: Path
    kept: Path
    what: str


@dataclass
class Staging:
    """One mode's part of a staged publication."""

    store_id: str
    root: Path
    stamp: str
    #: Puts the candidate at `store.opengwasdb.partial`, after the snapshot.
    stage: Callable[[], None]
    #: Undoes `stage`, whatever part of it ran; it runs on every rollback.
    unstage: Callable[[], None]
    #: Runs the mode's steps and verdict on the staged candidate and returns the marker.
    work: Callable[[Path], Mapping[str, Any]]
    #: Turns a refusal (`register.RegisterError`) and the rollback's notes into the mode's own.
    refuse: Callable[[register.RegisterError, str], BaseException]
    #: The records a refused call keeps; asked only after the snapshot exists.
    evidence: Callable[[], list[Evidence]] = lambda: []
    #: Records removed once the snapshot holds them, so any left after `work` are this call's.
    clear: tuple[Path, ...] = ()
    #: The test-only fault boundaries after the snapshot and after staging.
    boundaries: tuple[str, str] = ("staged-publication-snapshot-taken", "staged-publication-staged")
    #: Carries out the written marker and removes it last; the same call recovers it after a crash.
    #: By default `register.complete_publication`; the entry point's also clears Snakemake's metadata.
    finish: Callable[[], Any] | None = None

    @property
    def snapshot(self) -> Path:
        return paths.force_snapshot_path(self.store_id, self.stamp, root=self.root)


@contextmanager
def _shielded() -> Iterator[None]:
    """Ctrl-C and SIGTERM ignored, as `release._run` shields settlement, so a rollback runs to the end."""
    try:
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    except ValueError:  # not the main thread: no handler can interrupt it anyway
        previous = {}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _roll_back(s: Staging) -> str:
    """Undo the staging, keep the evidence, then restore `records/`; return what a refusal should add.

    The snapshot's path is known before it is copied, so a snapshot an
    interruption left just after its rename is still restored. Keeping
    evidence never stops the restore.
    """
    notes: list[str] = []
    with _shielded():
        try:
            s.unstage()
        finally:
            try:
                if s.snapshot.exists():
                    for item in s.evidence():
                        if not item.record.is_file():
                            continue
                        try:
                            item.kept.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(item.record, item.kept)
                            notes.append(f" The {item.what} is kept at {item.kept}.")
                        except Exception as exc:  # evidence only: never a reason to stop the rollback
                            notes.append(f" The {item.what} could not be kept ({exc!r}).")
            finally:
                if s.snapshot.exists():
                    run.restore_force_snapshot(s.snapshot)
    return "".join(notes)


def publish(s: Staging) -> None:
    """Snapshot, stage, work, marker, finalize; before the marker, any failure is rolled back.

    Raises the mode's refusal for a `register.RegisterError`, `Interrupted`
    (with the state it left) for Ctrl-C or SIGTERM, and anything else as it
    came. From the marker on nothing is undone: `finish` carries it out and
    removes it last, and the same call recovers it in the next run.
    """
    marker_p = paths.publication_marker(s.store_id, root=s.root)
    try:
        run.snapshot_records(s.store_id, s.root, s.stamp)
        run.fault_boundary(s.boundaries[0])
        for record in s.clear:
            record.unlink(missing_ok=True)
        s.stage()
        run.fault_boundary(s.boundaries[1])
        marker = s.work(s.snapshot)
        register.begin_publication(marker, s.root)
    except BaseException as exc:
        if os.path.lexists(marker_p):
            if isinstance(exc, KeyboardInterrupt) and not isinstance(exc, Interrupted):
                raise Interrupted(s.store_id, PENDING) from exc
            raise  # committed: `complete_publication` recovers it from the marker
        notes = _roll_back(s)
        if isinstance(exc, register.RegisterError):
            raise s.refuse(exc, notes) from exc
        if isinstance(exc, KeyboardInterrupt) and not isinstance(exc, Interrupted):
            raise Interrupted(s.store_id, ROLLED_BACK) from exc
        raise
    try:
        if s.finish is None:
            register.complete_publication(s.store_id, s.root)
        else:
            s.finish()
    except KeyboardInterrupt as exc:
        raise Interrupted(s.store_id, PENDING if os.path.lexists(marker_p) else PUBLISHED) from exc


def republication(
    *,
    b: bundle.Bundle,
    root: Path,
    phase: str,
    stamp: str,
    snapshot: Path,
    now: str,
    status: str,
    warnings: list[str],
    validator: engine.EngineIdentity,
    wrote_store: engine.EngineIdentity,
    engines: list[dict[str, Any]],
    observed: dict[str, Any],
    inputs: list[str],
    blocks: Mapping[str, Any],
    carried_validation: Mapping[str, Any] | None = None,
    carried_register: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The new Validation Record and `publication.json` of a Store republished over its predecessor.

    The record is `register`'s shape (`build_validation_record`): this run's
    verdict, the replaced record's `acceptance`, and the archive the old
    release goes to, then the `carried` blocks of the replaced records that
    are not already set, then the mode's `blocks`, in both records.
    """
    store_id = b.store_id
    archive = paths.replaced_dir(store_id, stamp, root=root)
    previous = b.validation if isinstance(b.validation, Mapping) else {}
    validation = register.build_validation_record(
        status=status,
        checks_store=status,
        validated_at=now,
        validator=validator,
        wrote_store=wrote_store,
        engines=engines,
        observed=observed,
        warnings=list(warnings),
        errors=[],
        acceptance=register.acceptance_evidence(previous),
        replaced={"archive": str(archive), "replaced_at": now},
    )
    register_step = REGISTER_STEPS[phase]
    val_yaml = b.root / "validation.yaml"
    register_record = register.build_register_record(
        step=register_step,
        store_id=store_id,
        at=now,
        inputs=inputs,
        outputs=[str(val_yaml)],
        wrote_store=wrote_store,
        record_path=paths.record_path(store_id, register_step, root=root),
        published_store=str(paths.store_path(store_id, root=root)),
        replaced_archive=str(archive),
    )
    for record, carried in ((validation, carried_validation), (register_record, carried_register)):
        for key, value in (carried or {}).items():
            if key not in record and key not in blocks:
                record[key] = value
        record.update(blocks)
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


__all__ = [
    "Evidence",
    "Interrupted",
    "PENDING",
    "PUBLISHED",
    "ROLLED_BACK",
    "Staging",
    "publish",
    "republication",
]
