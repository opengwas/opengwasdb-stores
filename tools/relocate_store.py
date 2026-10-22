#!/usr/bin/env python3
"""Move a published release from one Artifact Root to another, its step records with it (#331).

    pixi run -e dev python3 tools/relocate_store.py OGS-00015 \\
        --from /data/opengwasdb/work/epic232/221/dev-builds --to /data/opengwasdb/stores [--dry-run | --resume]

A step record names the artifact root its step ran under, and `register`
holds a step's executed argv to the plan under the root it runs at
(`register.check_argv_drift`). A Store Release's directory under the
Artifact Root, renamed into another root, therefore still looks up to date to
the release workflow, which decides by file times, and is refused only at its
next register, as drift naming the old root. This tool renames
`<old-root>/<id>` to `<new-root>/<id>` and moves the paths its records name
with it:

- In `records/*.json` it rewrites only the fields that name where a step ran
  (`RECORD_PATH_FIELDS`): a string that is `<old-root>/<id>` or a path under it
  is put under `<new-root>/<id>`. `stdout`, `stderr` and every other field are
  left as the step wrote them, and so is `work/analyses.exclusions.json`: they
  are the history of what ran, and nothing checks the paths they mention.
- Each rewritten record is written as `run` writes one (`run.record_text`),
  and keeps its mtime: the workflow decides what to rerun from the order of
  those mtimes.
- `<id>/relocations/<UTC>.json` is the receipt: both roots, each record's
  sha256 before and after, and how many strings each field moves. It is
  written, `status: started`, before anything moves, and marked `complete`
  once every record is rewritten. Any interruption in between, Ctrl-C
  included, leaves records that may name both roots; the tool then names the
  receipt, refuses to start another move of the release, and `--resume`
  finishes this one. Before it moves or writes anything, `--resume` checks
  that exactly one unfinished receipt names this move, that only one of the
  two directories exists, that no publication is pending and no Store staged,
  and that `records/` holds exactly the receipt's records, each as it was or as
  planned. It then rewrites only those still as they were.

It refuses, moving and writing nothing, while a relocation of the release is
unfinished, a publication is pending (`publication.json`) or a Store is staged
(`store.opengwasdb.partial*`), when the target exists, the roots are on
different filesystems, or a run holds the release's lock at either root, when
a record names an artifact path of another root or release, or names the old
root anywhere this tool does not rewrite, when a record would not be written
back byte for byte, and when `opengwasdb info` reports another id for the
Store. Without `opengwasdb` on PATH that last check is skipped, and the output
and the receipt say so. The checks are made again, the Store id included,
under the release's lock at both roots, which the tool holds until the
receipt is complete. `--dry-run` makes them without the lock and prints what
would change.

It exits 2 when it refuses, having changed nothing, and 1 when a move it
started did not finish.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ogstores import paths, release, run

# The record fields that name where a step ran; nothing else is rewritten.
RECORD_PATH_FIELDS: tuple[str, ...] = ("argv", "planned_argv", "inputs", "outputs", "record_path", "published_store")

# What a step printed: history, left as written and not checked.
RECORD_OUTPUT_FIELDS: tuple[str, ...] = ("stdout", "stderr")

RECEIPTS_DIRNAME: str = "relocations"
STARTED, COMPLETE = "started", "complete"
REFUSED_EXIT, UNFINISHED_EXIT = 2, 1

# Test-only, after `run.fault_boundary`'s variables: the record replacement
# (1-based) before which a move raises, as a failed write would, or `receipt`
# for a failure while the receipt is first written.
FAIL_AT_ENV: str = "OGSTORES_TEST_RELOCATION_FAIL_AT"
RECEIPT_FAULT: str = "receipt"


class Refusal(Exception):
    """The relocation cannot go ahead, and this run changed nothing.

    `state` replaces the usual account of what is on disk, when this run had
    to undo something it began.
    """

    def __init__(self, reason: str, state: str | None = None) -> None:
        super().__init__(reason)
        self.state = state


class Unfinished(Exception):
    """A relocation started and did not finish; its receipt says what is left."""

    def __init__(self, store_id: str, old_root: Path, new_root: Path, receipt_p: Path, cause: BaseException) -> None:
        super().__init__(
            f"the move of {store_id} did not finish: {cause}\n"
            f"Its records may name both {old_root} and {new_root}. {receipt_p} records what each record was "
            "and is to become. Finish the move with\n"
            f"  python3 tools/relocate_store.py {store_id} --from {old_root} --to {new_root} --resume\n"
            f"Until then this tool refuses to start another move of {store_id}."
        )


def _release_entries() -> frozenset[str]:
    """The names Store Release Build gives inside a Store Release's directory under its Artifact Root, `<root>/<id>/`.

    A path `<root>/<some-id>/<one of these>...` is an artifact path a step
    derived from its root; `<registry>/<id>/validation.yaml` is not.
    """
    probe = "OGS-00000"
    release_dir = paths.store_dir(probe, root="/")
    return frozenset(
        p.relative_to(release_dir).parts[0]
        for p in (
            paths.store_path(probe, root="/"),
            paths.partial_store_path(probe, root="/"),
            paths.backup_store_path(probe, root="/"),
            paths.completion_checkpoint_path(probe, root="/"),
            paths.records_dir(probe, root="/"),
            paths.work_dir(probe, root="/"),
            paths.tmp_dir(probe, root="/"),
            paths.publication_marker(probe, root="/"),
            release_dir / paths.REPLACED_DIRNAME,
        )
    )


RELEASE_ENTRIES: frozenset[str] = _release_entries()


def artifact_location(value: str) -> tuple[PurePosixPath, str] | None:
    """The (root, release id) of an artifact path, or None for a string that is not one."""
    parts = PurePosixPath(value).parts
    for i in range(1, len(parts) - 1):
        entry = parts[i + 1]
        if paths.is_valid_store_id(parts[i]) and (
            entry in RELEASE_ENTRIES or entry.startswith(paths.FORCE_SNAPSHOT_PREFIX)
        ):
            return PurePosixPath(*parts[:i]), parts[i]
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def _mentions(text: str, directory: str) -> int:
    """How often `text` mentions `directory` itself or a path under it, not a sibling such as `<directory>-old`."""
    count, start = 0, text.find(directory)
    while start != -1:
        after = text[start + len(directory): start + len(directory) + 1]
        count += not (after.isalnum() or after in "-_")
        start = text.find(directory, start + 1)
    return count


def _under(value: str, directory: str) -> bool:
    return value == directory or value.startswith(directory + "/")


def _move(value: Any, old_dir: str, new_dir: str) -> Any:
    """`value` with every string that is `old_dir` or a path under it put under `new_dir`."""
    if isinstance(value, str):
        return new_dir + value[len(old_dir):] if _under(value, old_dir) else value
    if isinstance(value, list):
        return [_move(item, old_dir, new_dir) for item in value]
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RecordMove:
    path: Path
    before: bytes
    after: str
    replaced: dict[str, int]
    left_as_written: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.path.parent.name}/{self.path.name}"

    @property
    def changed(self) -> bool:
        return any(self.replaced.values())


@dataclass
class Relocation:
    store_id: str
    old_root: Path
    new_root: Path
    records: list[RecordMove]
    store_id_check: str

    @property
    def old_dir(self) -> Path:
        return paths.store_dir(self.store_id, root=self.old_root)

    @property
    def new_dir(self) -> Path:
        return paths.store_dir(self.store_id, root=self.new_root)

    def totals(self) -> dict[str, int]:
        return {name: sum(move.replaced[name] for move in self.records) for name in RECORD_PATH_FIELDS}


def plan_record(record_p: Path, store_id: str, old_root: Path, new_root: Path) -> RecordMove:
    """What relocating moves in one record; a Refusal if it cannot be moved exactly."""
    old_dir = str(paths.store_dir(store_id, root=old_root))
    new_dir = str(paths.store_dir(store_id, root=new_root))
    name = f"{record_p.parent.name}/{record_p.name}"
    before = record_p.read_bytes()
    try:
        text = before.decode("utf-8")
        record = json.loads(text)
    except ValueError as exc:
        raise Refusal(f"{name} is not a readable JSON record: {exc}") from exc
    if not isinstance(record, dict):
        raise Refusal(f"{name} is not a JSON object")
    if run.record_text(record) != text:
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True), run.record_text(record).splitlines(keepends=True),
            f"{name} as written", f"{name} as `run` writes it", n=1,
        ))
        raise Refusal(f"{name} would not be written back byte for byte:\n{diff}")

    problems = []
    for key in RECORD_PATH_FIELDS:
        for value in _strings(record.get(key)):
            location = artifact_location(value)
            if location is not None and location != (PurePosixPath(old_root), store_id):
                problems.append(f"{name}: {key} names {value}, under {location[0]}/{location[1]}, not {old_dir}")
    moved = {
        key: _move(value, old_dir, new_dir) if key in RECORD_PATH_FIELDS else value for key, value in record.items()
    }
    replaced = {key: sum(_under(s, old_dir) for s in _strings(record.get(key))) for key in RECORD_PATH_FIELDS}
    left_as_written = {}
    for key, value in moved.items():
        mentions = sum(_mentions(s, old_dir) for s in _strings(value))
        if not mentions:
            continue
        if key in RECORD_OUTPUT_FIELDS:
            left_as_written[key] = mentions
        else:
            problems.append(f"{name}: {key} still names {old_dir} where this tool does not rewrite it")
    if problems:
        raise Refusal("\n".join(problems))
    return RecordMove(record_p, before, run.record_text(moved), replaced, left_as_written)


def store_id_check(store_p: Path, store_id: str) -> str:
    """What `opengwasdb info` says the Store's id is; a Refusal if it is another id."""
    executable = shutil.which("opengwasdb")
    if executable is None:
        return "not checked: opengwasdb is not on PATH"
    argv = [executable, "info", str(store_p), "--format", "json"]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise Refusal(f"`opengwasdb info {store_p}` failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        reported = json.loads(result.stdout).get("store_id")
    except (ValueError, AttributeError) as exc:
        raise Refusal(f"`opengwasdb info {store_p}` did not print a JSON object: {exc}") from exc
    if reported != store_id:
        raise Refusal(f"`opengwasdb info {store_p}` reports store_id {reported!r}, not {store_id}")
    return f"opengwasdb info reports store_id {reported}"


def unfinished_receipts(store_id: str, roots: tuple[Path, ...]) -> list[Path]:
    """The receipts, at any of `roots`, of relocations of `store_id` that are not complete."""
    found = []
    for root in roots:
        for receipt_p in sorted((paths.store_dir(store_id, root=root) / RECEIPTS_DIRNAME).glob("*.json")):
            try:
                status = json.loads(receipt_p.read_text(encoding="utf-8")).get("status")
            except (OSError, ValueError, AttributeError):
                status = None
            if status != COMPLETE:
                found.append(receipt_p)
    return found


def _pending(store_id: str, store_dir: Path) -> list[str]:
    """A pending publication or a staged Store in a Store Release's directory under its Artifact Root."""
    problems = []
    marker = store_dir / paths.PUBLICATION_MARKER
    if os.path.lexists(marker):
        problems.append(f"{marker} exists: a publication is pending, and `pixi run release {store_id}` finishes it")
    for staged in sorted(store_dir.glob("store.opengwasdb.partial*")) + sorted(store_dir.glob(".store.opengwasdb.partial*")):
        problems.append(f"{staged} exists: a Store is staged")
    return problems


def fault_at() -> int | str | None:
    """The test fault `OGSTORES_TEST_RELOCATION_FAIL_AT` asks for, read before anything moves."""
    value = os.environ.get(FAIL_AT_ENV, "")
    if not value:
        return None
    if value == RECEIPT_FAULT or (value.isdigit() and int(value) > 0):
        return value if value == RECEIPT_FAULT else int(value)
    raise Refusal(f"{FAIL_AT_ENV}={value!r} names neither a record replacement (1, 2, ...) nor `{RECEIPT_FAULT}`")


def preflight(store_id: str, old_root: Path, new_root: Path, *, locked: bool = False) -> Relocation:
    """Every check, reading only; a Refusal listing each reason the relocation cannot go ahead.

    `locked` says this tool holds the release's lock at both roots, so they
    are not checked again; everything else is.
    """
    unfinished = unfinished_receipts(store_id, (old_root, new_root))
    if unfinished:
        raise Refusal("\n".join(
            f"{receipt_p} records a move of {store_id} that did not finish; finish it with --resume"
            for receipt_p in unfinished
        ))
    old_dir = paths.store_dir(store_id, root=old_root)
    new_dir = paths.store_dir(store_id, root=new_root)
    problems: list[str] = []
    if old_root == new_root:
        problems.append(f"the old and new roots are the same, {old_root}")
    if old_dir.is_symlink() or not old_dir.is_dir():
        raise Refusal(f"{old_root} holds no directory for {store_id} ({old_dir})")
    store_p = paths.store_path(store_id, root=old_root)
    if not store_p.is_dir():
        problems.append(f"no published Store at {store_p}")
    if not new_root.is_dir():
        problems.append(f"the new root {new_root} is not a directory")
    elif old_dir.stat().st_dev != new_root.stat().st_dev:
        problems.append(f"{old_dir} and {new_root} are on different filesystems, so a rename cannot move it")
    if os.path.lexists(new_dir):
        problems.append(f"{new_dir} already exists")
    receipts = old_dir / RECEIPTS_DIRNAME
    if os.path.lexists(receipts) and (receipts.is_symlink() or not receipts.is_dir()):
        problems.append(f"{receipts} is not a directory, so the receipt cannot be written there")
    problems += _pending(store_id, old_dir)
    for root in () if locked else (old_root, new_root):
        holder = release.lock_holder(store_id, root) if root.is_dir() else None
        if holder is not None:
            problems.append(f"a run of {store_id} holds {paths.release_lock_path(store_id, root=root)} ({holder})")

    records: list[RecordMove] = []
    for record_p in sorted(paths.records_dir(store_id, root=old_root).glob("*.json")):
        try:
            records.append(plan_record(record_p, store_id, old_root, new_root))
        except Refusal as exc:
            problems.append(str(exc))
    if not records:
        problems.append(f"no step records under {paths.records_dir(store_id, root=old_root)}")

    checked = "not checked"
    if store_p.is_dir():
        try:
            checked = store_id_check(store_p, store_id)
        except Refusal as exc:
            problems.append(str(exc))
    if problems:
        raise Refusal("\n".join(problems))
    return Relocation(store_id, old_root, new_root, records, checked)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace(path: Path, text: str, like: os.stat_result) -> None:
    """Replace `path` with `text` atomically, with the mode and times of `like`."""
    temp = path.with_name(f".{path.name}.relocating.{os.getpid()}")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, like.st_mode & 0o7777)
    os.utime(temp, ns=(like.st_atime_ns, like.st_mtime_ns))
    os.replace(temp, path)


def _receipt_temp(receipt_p: Path) -> Path:
    return receipt_p.with_name(f".{receipt_p.name}.{os.getpid()}")


def _write_receipt(receipt_p: Path, document: dict[str, Any], *, fault: bool = False) -> None:
    """Write a receipt atomically, so a reader sees the whole of one state or of the next."""
    temp = _receipt_temp(receipt_p)
    temp.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    with open(temp, "rb") as handle:
        os.fsync(handle.fileno())
    if fault:
        raise OSError(f"test fault while writing the receipt ({FAIL_AT_ENV})")
    os.replace(temp, receipt_p)
    _fsync_dir(receipt_p.parent)


def start(receipt_p: Path, document: dict[str, Any], fault: int | str | None) -> None:
    """Write the receipt of a move about to start; if that fails, undo it and refuse.

    Nothing has moved yet, so what this run wrote of the receipt, and the
    `relocations/` directory if this run made it, are removed, and the
    refusal says what, if anything, could not be.
    """
    receipts = receipt_p.parent
    made = not os.path.lexists(receipts)
    try:
        receipts.mkdir(exist_ok=True)
        _write_receipt(receipt_p, document, fault=fault == RECEIPT_FAULT)
    except BaseException as exc:
        written = [_receipt_temp(receipt_p), receipt_p] + ([receipts] if made else [])
        for path in written:
            with contextlib.suppress(OSError):
                if path == receipts:
                    path.rmdir()
                else:
                    path.unlink()
        remaining = [str(path) for path in written if os.path.lexists(path)]
        state = (
            f"Nothing was moved, but this run could not remove {', '.join(remaining)}."
            if remaining else "Nothing was moved or written."
        )
        raise Refusal(f"the receipt {receipt_p} could not be written: {exc}", state=state) from exc


def _tool_commit() -> str | None:
    result = subprocess.run(["git", "-C", str(paths.REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def receipt(relocation: Relocation, started_at: str) -> dict[str, Any]:
    """The receipt of a relocation about to start."""
    left = {move.name: move.left_as_written for move in relocation.records if move.left_as_written}
    sidecar = paths.build_manifest_sidecar_path(relocation.store_id, root=relocation.old_root)
    mentions = _mentions(sidecar.read_text(encoding="utf-8"), str(relocation.old_dir)) if sidecar.is_file() else 0
    if mentions:
        left[f"{sidecar.parent.name}/{sidecar.name}"] = {"text": mentions}
    return {
        "status": STARTED,
        "store_id": relocation.store_id,
        "old_root": str(relocation.old_root),
        "new_root": str(relocation.new_root),
        "started_at": started_at,
        "completed_at": None,
        "resumed_at": [],
        "tool": {"path": "tools/relocate_store.py", "commit": _tool_commit()},
        "store_id_check": relocation.store_id_check,
        "rewritten_fields": list(RECORD_PATH_FIELDS),
        "records": [
            {
                "path": move.name,
                "sha256_before": _sha256(move.before),
                "sha256_after": _sha256(move.after.encode("utf-8")),
                "replaced": move.replaced,
            }
            for move in relocation.records
        ],
        "replaced": relocation.totals(),
        "left_as_written": left,
    }


def describe(relocation: Relocation, document: dict[str, Any]) -> str:
    lines = [
        f"relocate {relocation.store_id}",
        f"  from {relocation.old_dir}",
        f"  to   {relocation.new_dir}",
        f"  Store id: {relocation.store_id_check}",
    ]
    for entry in document["records"]:
        counts = ", ".join(f"{name} {n}" for name, n in entry["replaced"].items() if n) or "nothing to move"
        lines.append(f"  {entry['path']}: {counts}")
    totals = document["replaced"]
    lines.append(
        f"  total: {sum(totals.values())} strings in {sum(1 for m in relocation.records if m.changed)} of "
        f"{len(relocation.records)} records: " + ", ".join(f"{name} {n}" for name, n in totals.items())
    )
    for path, mentions in document["left_as_written"].items():
        what = ", ".join(f"{key} {n}" for key, n in mentions.items())
        lines.append(f"  left as written, naming the old root: {path} ({what})")
    return "\n".join(lines)


@contextlib.contextmanager
def _locked(store_id: str, roots: tuple[Path, ...]) -> Iterator[None]:
    """The release's lock at each of `roots`, taken without waiting, as `pixi run release` takes it."""
    locks = [release.ReleaseLocks(root) for root in roots]
    try:
        for lock in locks:
            try:
                lock.acquire(store_id)
            except release.ReleaseInProgressError as exc:
                raise Refusal(str(exc)) from exc
        yield
    finally:
        for lock in locks:
            lock.release_all()


def carry_out(
    store_id: str,
    old_root: Path,
    new_root: Path,
    receipt_p: Path,
    document: dict[str, Any],
    writes: list[tuple[str, str]],
    fault: int | str | None,
) -> Path:
    """Finish a move its receipt has started: rename, write each record, and mark the receipt complete.

    The Store Release's directory is renamed only if the receipt is still at
    the old root. `writes` are each record's path under that directory and
    its new text, all checked before this is called. Any interruption, Ctrl-C
    included, is `Unfinished`, naming the receipt. Returns where it is.
    """
    old_dir, new_dir = paths.store_dir(store_id, root=old_root), paths.store_dir(store_id, root=new_root)
    moved_receipt = new_dir / receipt_p.relative_to(old_dir) if receipt_p.is_relative_to(old_dir) else receipt_p
    try:
        if moved_receipt != receipt_p:
            old_dir.rename(new_dir)
            _fsync_dir(old_root)
            _fsync_dir(new_root)
        for n, (name, text) in enumerate(writes, start=1):
            if n == fault:
                raise OSError(f"test fault before replacement {n} ({FAIL_AT_ENV})")
            record_p = new_dir / name
            _replace(record_p, text, record_p.stat())
        _fsync_dir(paths.records_dir(store_id, root=new_root))
        document["status"] = COMPLETE
        document["completed_at"] = _now()
        _write_receipt(moved_receipt, document)
    except BaseException as exc:
        where = moved_receipt if os.path.lexists(moved_receipt) else receipt_p
        raise Unfinished(store_id, old_root, new_root, where, exc) from exc
    return moved_receipt


def relocate(store_id: str, old_root: Path, new_root: Path, *, dry_run: bool) -> int:
    fault = fault_at()
    relocation = preflight(store_id, old_root, new_root)
    stamp = run.utc_stamp()
    started_at = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    print(describe(relocation, receipt(relocation, started_at)))
    if dry_run:
        print("Dry run: nothing was moved or written.")
        return 0

    with _locked(store_id, (old_root, new_root)):
        relocation = preflight(store_id, old_root, new_root, locked=True)
        document = receipt(relocation, started_at)
        receipt_p = relocation.old_dir / RECEIPTS_DIRNAME / f"{stamp}.json"
        start(receipt_p, document, fault)
        writes = [(move.name, move.after) for move in relocation.records if move.changed]
        receipt_p = carry_out(store_id, old_root, new_root, receipt_p, document, writes, fault)
    print(f"moved; receipt {receipt_p}")
    return 0


def resume_plan(store_id: str, old_root: Path, new_root: Path) -> tuple[Path, dict[str, Any], list[tuple[str, str]]]:
    """Check that the one unfinished move can be finished as its receipt planned; what is left to write.

    Reads only, and refuses with every reason it finds: the receipt is the
    only unfinished one and names this move, only one of the two directories
    exists, no publication is pending and no Store staged, and `records/` holds
    exactly the receipt's records, each as it was or as planned.
    """
    unfinished = unfinished_receipts(store_id, (old_root, new_root))
    if not unfinished:
        raise Refusal(f"there is no unfinished move of {store_id} between {old_root} and {new_root}")
    if len(unfinished) > 1:
        raise Refusal(
            f"{len(unfinished)} unfinished moves of {store_id} are recorded, so which to finish is not clear: "
            + ", ".join(map(str, unfinished))
        )
    receipt_p = unfinished[0]
    try:
        document = json.loads(receipt_p.read_text(encoding="utf-8"))
        recorded = (document.get("store_id"), document.get("old_root"), document.get("new_root"))
        entries = {entry["path"]: entry for entry in document["records"]}
    except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
        raise Refusal(f"the receipt {receipt_p} cannot be read: {exc!r}") from exc
    if recorded != (store_id, str(old_root), str(new_root)):
        raise Refusal(
            f"{receipt_p} moves {recorded[0]} from {recorded[1]} to {recorded[2]}, not from {old_root} to {new_root}"
        )
    old_dir, new_dir = paths.store_dir(store_id, root=old_root), paths.store_dir(store_id, root=new_root)
    if os.path.lexists(old_dir) and os.path.lexists(new_dir):
        raise Refusal(f"both {old_dir} and {new_dir} exist, so the move cannot be finished by a rename")
    here = old_dir if receipt_p.is_relative_to(old_dir) else new_dir

    problems = _pending(store_id, here)
    present = {f"{p.parent.name}/{p.name}" for p in (here / "records").glob("*.json")}
    problems += [f"{here / name} is not in the receipt {receipt_p}" for name in sorted(present - entries.keys())]
    problems += [f"{here / name}, in the receipt {receipt_p}, is missing" for name in sorted(entries.keys() - present)]
    writes = []
    for name, entry in entries.items():
        record_p = here / name
        if name not in present:
            continue
        current = _sha256(record_p.read_bytes())
        if current == entry.get("sha256_after"):
            continue
        if current != entry.get("sha256_before"):
            problems.append(f"{record_p} is neither the record {receipt_p} started from nor the one it planned")
            continue
        try:
            move = plan_record(record_p, store_id, old_root, new_root)
        except Refusal as exc:
            problems.append(str(exc))
            continue
        if _sha256(move.after.encode("utf-8")) != entry.get("sha256_after"):
            problems.append(f"{record_p} would not become the record {receipt_p} planned")
            continue
        writes.append((name, move.after))
    if problems:
        raise Refusal("\n".join(problems))
    return receipt_p, document, writes


def resume(store_id: str, old_root: Path, new_root: Path) -> int:
    """Finish the one unfinished relocation of `store_id` from `old_root` to `new_root`."""
    fault = fault_at()
    with _locked(store_id, (old_root, new_root)):
        receipt_p, document, writes = resume_plan(store_id, old_root, new_root)
        document["resumed_at"].append(_now())
        receipt_p = carry_out(store_id, old_root, new_root, receipt_p, document, writes, fault)
    print(f"moved; receipt {receipt_p}")
    return 0


def refusal_state(store_id: str, old_root: Path, new_root: Path) -> str:
    """What is on disk after a refusal: an unfinished move is never reported as nothing moved."""
    unfinished = unfinished_receipts(store_id, (old_root, new_root))
    if unfinished:
        return f"This run changed nothing; {', '.join(map(str, unfinished))} records a move that has not finished."
    return "Nothing was moved or written."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id", help="the Store Release to move, e.g. OGS-00015")
    parser.add_argument("--from", dest="old_root", required=True, type=Path, help="the Artifact Root it is in now")
    parser.add_argument("--to", dest="new_root", required=True, type=Path, help="the Artifact Root to move it into")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="check and print what would change; write nothing")
    mode.add_argument("--resume", action="store_true", help="finish a move that did not finish, from its receipt")
    args = parser.parse_args(argv)
    store_id = args.store_id
    old_root, new_root = Path(os.path.abspath(args.old_root)), Path(os.path.abspath(args.new_root))
    try:
        if not paths.is_valid_store_id(store_id):
            raise Refusal(f"{store_id!r} is not a Store Release id (OGS- and five digits)")
        if args.resume:
            return resume(store_id, old_root, new_root)
        return relocate(store_id, old_root, new_root, dry_run=args.dry_run)
    except Refusal as exc:
        state = exc.state or refusal_state(store_id, old_root, new_root)
        print(f"relocate_store: refusing to move {store_id}:\n{exc}\n{state}", file=sys.stderr)
        return REFUSED_EXIT
    except Unfinished as exc:
        print(f"relocate_store: {exc}", file=sys.stderr)
        return UNFINISHED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
