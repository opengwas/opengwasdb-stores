#!/usr/bin/env python3
"""Move a published release from one Artifact Root to another, its step records with it (#331).

    pixi run -e dev python3 tools/relocate_store.py OGS-00015 \\
        --from /data/opengwasdb/work/epic232/221/dev-builds --to /data/opengwasdb/stores [--dry-run]

A step record names the artifact root its step ran under, and `register`
holds a step's executed argv to the plan under the root it runs at
(`register.check_argv_drift`). A release directory renamed into another root
therefore still looks up to date to the release workflow, which decides by
file times, and is refused only at its next register, as drift naming the old
root. This tool renames `<old-root>/<id>` to `<new-root>/<id>` and moves the
paths its records name with it:

- In `records/*.json` it rewrites only the fields that name where a step ran
  (`RECORD_PATH_FIELDS`): a string that is `<old-root>/<id>` or a path under it
  is put under `<new-root>/<id>`. `stdout`, `stderr` and every other field are
  left as the step wrote them, and so is `work/analyses.exclusions.json`: they
  are the history of what ran, and nothing checks the paths they mention.
- Each rewritten record is written as `run` writes one, and keeps its mtime:
  the workflow decides what to rerun from the order of those mtimes.
- `<new-root>/<id>/relocations/<UTC>.json` is the receipt: both roots, each
  record's sha256 before and after, and how many strings each field had moved.

It refuses, moving and writing nothing, while a publication is pending
(`publication.json`) or a Store is staged (`store.opengwasdb.partial*`), when
the target exists, the roots are on different filesystems, or a run holds the
release's lock at either root, when a record names an artifact path of another
root or release, or names the old root anywhere this tool does not rewrite,
when a record would not be written back byte for byte, and when
`opengwasdb info` reports another id for the Store. Without `opengwasdb` on
PATH that last check is skipped, and the output and the receipt say so.
`--dry-run` makes the same checks and prints what would change.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ogstores import paths, release, run

# The record fields that name where a step ran; nothing else is rewritten.
RECORD_PATH_FIELDS: tuple[str, ...] = ("argv", "planned_argv", "inputs", "outputs", "record_path", "published_store")

# What a step printed: history, left as written and not checked.
RECORD_OUTPUT_FIELDS: tuple[str, ...] = ("stdout", "stderr")

RECEIPTS_DIRNAME: str = "relocations"


class Refusal(Exception):
    """The relocation cannot go ahead; nothing was moved or written."""


def _release_entries() -> frozenset[str]:
    """The names Store Release Build gives inside a release directory, `<root>/<id>/`.

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


def serialise(record: dict[str, Any]) -> str:
    """A record's bytes as `run._write_record_atomically` writes them."""
    return json.dumps(record, indent=2, sort_keys=False) + "\n"


@dataclass
class RecordMove:
    path: Path
    before: bytes
    after: str
    replaced: dict[str, int]
    left_as_written: dict[str, int] = field(default_factory=dict)

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


def _under(value: str, directory: str) -> bool:
    return value == directory or value.startswith(directory + "/")


def _move(value: Any, old_dir: str, new_dir: str) -> Any:
    """`value` with every string that is `old_dir` or a path under it put under `new_dir`."""
    if isinstance(value, str):
        return new_dir + value[len(old_dir):] if _under(value, old_dir) else value
    if isinstance(value, list):
        return [_move(item, old_dir, new_dir) for item in value]
    return value


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
    if serialise(record) != text:
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True), serialise(record).splitlines(keepends=True),
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
    return RecordMove(record_p, before, serialise(moved), replaced, left_as_written)


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


def preflight(store_id: str, old_root: Path, new_root: Path, *, checked: str | None = None) -> Relocation:
    """Every check, reading only; a Refusal listing each reason the relocation cannot go ahead.

    `checked` is an earlier pass's Store id check, made again under this
    tool's own release locks: the locks are then not checked, and the Store's
    id is not read again.
    """
    old_dir = paths.store_dir(store_id, root=old_root)
    new_dir = paths.store_dir(store_id, root=new_root)
    problems: list[str] = []
    if old_root == new_root:
        problems.append(f"the old and new roots are the same, {old_root}")
    if old_dir.is_symlink() or not old_dir.is_dir():
        raise Refusal(f"no release directory at {old_dir}")
    store_p = paths.store_path(store_id, root=old_root)
    if not store_p.is_dir():
        problems.append(f"no published Store at {store_p}")
    if not new_root.is_dir():
        problems.append(f"the new root {new_root} is not a directory")
    elif old_dir.stat().st_dev != new_root.stat().st_dev:
        problems.append(f"{old_dir} and {new_root} are on different filesystems, so a rename cannot move it")
    if os.path.lexists(new_dir):
        problems.append(f"{new_dir} already exists")
    marker = paths.publication_marker(store_id, root=old_root)
    if os.path.lexists(marker):
        problems.append(f"{marker} exists: a publication is pending, and `pixi run release {store_id}` finishes it")
    for staged in sorted(old_dir.glob("store.opengwasdb.partial*")) + sorted(old_dir.glob(".store.opengwasdb.partial*")):
        problems.append(f"{staged} exists: a Store is staged")
    for root in (old_root, new_root) if checked is None else ():
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

    if checked is None and store_p.is_dir():
        try:
            checked = store_id_check(store_p, store_id)
        except Refusal as exc:
            problems.append(str(exc))
    if problems:
        raise Refusal("\n".join(problems))
    return Relocation(store_id, old_root, new_root, records, checked or "not checked")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_keeping_times(path: Path, text: str, like: os.stat_result) -> None:
    """Replace `path` with `text` atomically, with the mode and times of the file it replaces."""
    temp = path.with_name(f".{path.name}.relocating.{os.getpid()}")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, like.st_mode & 0o7777)
    os.utime(temp, ns=(like.st_atime_ns, like.st_mtime_ns))
    os.replace(temp, path)


def _tool_commit() -> str | None:
    result = subprocess.run(["git", "-C", str(paths.REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def receipt(relocation: Relocation, relocated_at: str) -> dict[str, Any]:
    left = {
        f"{move.path.parent.name}/{move.path.name}": move.left_as_written
        for move in relocation.records
        if move.left_as_written
    }
    sidecar = paths.build_manifest_sidecar_path(relocation.store_id, root=relocation.old_root)
    mentions = _mentions(sidecar.read_text(encoding="utf-8"), str(relocation.old_dir)) if sidecar.is_file() else 0
    if mentions:
        left[f"{sidecar.parent.name}/{sidecar.name}"] = {"text": mentions}
    return {
        "store_id": relocation.store_id,
        "relocated_at": relocated_at,
        "old_root": str(relocation.old_root),
        "new_root": str(relocation.new_root),
        "tool": {"path": "tools/relocate_store.py", "commit": _tool_commit()},
        "store_id_check": relocation.store_id_check,
        "rewritten_fields": list(RECORD_PATH_FIELDS),
        "records": [
            {
                "path": f"{move.path.parent.name}/{move.path.name}",
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


def relocate(store_id: str, old_root: Path, new_root: Path, *, dry_run: bool) -> int:
    if not paths.is_valid_store_id(store_id):
        raise Refusal(f"{store_id!r} is not a Store Release id (OGS- and five digits)")
    relocation = preflight(store_id, old_root, new_root)
    stamp = run.utc_stamp()
    relocated_at = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    document = receipt(relocation, relocated_at)
    print(describe(relocation, document))
    if dry_run:
        print("Dry run: nothing was moved or written.")
        return 0

    # Held from the last check to the receipt, so no release of this id starts at either root meanwhile.
    locks = [release.ReleaseLocks(root) for root in (old_root, new_root)]
    try:
        for lock in locks:
            try:
                lock.acquire(store_id)
            except release.ReleaseInProgressError as exc:
                raise Refusal(str(exc)) from exc
        relocation = preflight(store_id, old_root, new_root, checked=relocation.store_id_check)
        document = receipt(relocation, relocated_at)
        old_dir, new_dir = relocation.old_dir, relocation.new_dir
        old_dir.rename(new_dir)
        _fsync_dir(old_root)
        _fsync_dir(new_root)
        for move in relocation.records:
            if move.changed:
                moved_p = new_dir / move.path.relative_to(old_dir)
                _write_keeping_times(moved_p, move.after, moved_p.stat())
        _fsync_dir(paths.records_dir(store_id, root=new_root))
        receipt_p = new_dir / RECEIPTS_DIRNAME / f"{stamp}.json"
        receipt_p.parent.mkdir(exist_ok=True)
        receipt_p.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    finally:
        for lock in locks:
            lock.release_all()
    print(f"moved; receipt {receipt_p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id", help="the release to move, e.g. OGS-00015")
    parser.add_argument("--from", dest="old_root", required=True, type=Path, help="the Artifact Root it is in now")
    parser.add_argument("--to", dest="new_root", required=True, type=Path, help="the Artifact Root to move it into")
    parser.add_argument("--dry-run", action="store_true", help="check and print what would change; write nothing")
    args = parser.parse_args(argv)
    try:
        return relocate(
            args.store_id, Path(os.path.abspath(args.old_root)), Path(os.path.abspath(args.new_root)), dry_run=args.dry_run
        )
    except Refusal as exc:
        print(f"relocate_store: refusing to move {args.store_id}:\n{exc}\nNothing was moved or written.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
