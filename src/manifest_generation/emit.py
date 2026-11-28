#!/usr/bin/env python3
"""Emit a candidate Release Bundle into the Store Registry (#248, ADR 0035).

Every Manifest Generator emits its candidate through here, and nothing else
writes one into `stores/`. A generator renders the bundle's content; the
emitter owns how it lands:

- every YAML document is written in one style, with `status: candidate` and
  the generator commands that produced it;
- the bundle is staged under `stores/.staging/<id>/`, checked against the
  Release Bundle contract and Manifest Generation's own rules
  (`generation_rules`), and given its generated `summary.yaml`, before
  anything in `stores/<id>/` changes;
- it then replaces `stores/<id>/` in one step, and only if that is absent or
  a candidate.

One emit of an id runs at a time: each holds `stores/.staging/<id>.lock`.

The shared pipeline calls `emit()`. A directory rendered elsewhere is emitted
through the command line, which reads it as data and rewrites it in the
emitter's own style:

    python3 -m manifest_generation.emit OGS-00042 --from <dir> \\
        --command "<step that produced it>" [--command ...] [--registry-root stores]

**Increment mode** (ADR 0045 §3). `emit_increment()` and `--increment` write
an Increment Bundle, ``stores/<id>/increments/<nnnn>/``, into a validated
Reference-Completed Hybrid's bundle and touch no other file of it. The
increment is staged with a copy of its bundle, held to the contract with it,
and moved into place under the same per-id lock, as a candidate:

    python3 -m manifest_generation.emit OGS-00011 --increment --from <rendered increment> \\
        --selection <roster.tsv> --command "<step that produced it>"
    python3 -m manifest_generation.emit OGS-00011 --increment --drop ID=REASON [--drop ...] \\
        --reason TEXT [--defect --erratum-issue ISSUE --erratum-text TEXT]
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import errno
import fcntl
import os
import shutil
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ogstores import bundle, increment, index, paths
from manifest_generation import REPO_ROOT, generation_rules

#: The registry subdirectory candidates are staged under. Hidden, so
#: `bundle-check` and `index` never see a half-written bundle, and ignored by
#: git, which keeps the per-id lock files out of the working tree's changes.
STAGING_DIRNAME: str = ".staging"
CANDIDATE_STATUS: str = "candidate"

#: The files a bundle directory may hold, for the command line's `--from`.
RENDERED_FILES: frozenset[str] = frozenset({"release.yaml", "build.yaml", "analyses.tsv", "validation.yaml"})
#: The directories it may hold: the sidecars, and the Column Map a mapped bundle carries.
RENDERED_DIRECTORIES: frozenset[str] = frozenset({"sidecars", "column_maps"})


class EmitError(RuntimeError):
    """A candidate Release Bundle could not be emitted; `stores/<id>/` is unchanged."""


@dataclass(frozen=True)
class CandidateBundle:
    """One candidate Release Bundle: its rendered content and the commands that produced it."""

    store_id: str
    release: Mapping[str, Any]
    build: Mapping[str, Any]
    analyses_tsv: str
    #: The generator commands that produced it, in order; recorded as `generator.commands`.
    commands: Sequence[str]
    validation: Mapping[str, Any] | None = None
    #: File name under `sidecars/` -> its text.
    sidecars: Mapping[str, str] = field(default_factory=dict)
    #: File name under `column_maps/` -> its bytes: the Column Map `build.yaml` binds, when carried.
    column_maps: Mapping[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class IncrementCandidate:
    """One candidate Increment Bundle (ADR 0045 §2): the rows it adds, its drops, and how it was produced."""

    store_id: str
    reason: str
    #: The commands that produced it, in order; recorded as `generator.commands`.
    commands: Sequence[str]
    #: The rows it adds, in the bundle's columns; None for a drop-only increment.
    analyses_tsv: str | None = None
    #: File name under `sidecars/` -> its text, each with the bundle's header.
    sidecars: Mapping[str, str] = field(default_factory=dict)
    generator_version: str | None = None
    source_snapshot: Mapping[str, Any] | None = None
    #: The resolver version and settings digest its rows were resolved under.
    resolver: Mapping[str, Any] | None = None
    require_added: Sequence[str] = ()
    #: Each `{analysis_id, reason, defect}`.
    drops: Sequence[Mapping[str, Any]] = ()
    erratum: Mapping[str, Any] | None = None
    created_at: str | None = None
    #: The explicit roster its rows are reconciled to (ADR 0045 §3, #546 ruling 5): every listed id
    #: is a row, admitted or refused at resolution, and every row is listed. Required when it adds rows.
    selection: Sequence[str] | None = None
    #: Where the roster was read from, and its bytes' sha256, recorded in `increment.yaml`.
    selection_path: str | None = None
    selection_sha256: str | None = None


#: The column of a roster file that names each Analysis: `analysis_id`, or `accession` as #557's joiners table has it.
SELECTION_COLUMNS: tuple[str, ...] = ("analysis_id", "accession")


def read_selection(path: Path | str) -> tuple[str, ...]:
    """The ids of an increment's roster file, in order; refuses a blank, duplicate or missing id column."""
    path = Path(path)
    try:
        header, body = bundle.read_sidecar_rows(path)
    except (OSError, UnicodeDecodeError, ValueError, csv.Error) as exc:
        raise EmitError(f"selection {path} {exc}") from exc
    column = next((c for c in SELECTION_COLUMNS if c in header), None)
    if column is None:
        raise EmitError(f"selection {path} names no {' or '.join(SELECTION_COLUMNS)} column")
    ids = [row[header.index(column)].strip() for row in body]
    if not ids:
        raise EmitError(f"selection {path} lists no Analysis")
    if any(not a for a in ids):
        raise EmitError(f"selection {path} has a blank {column}")
    duplicates = sorted({a for a in ids if ids.count(a) > 1})
    if duplicates:
        raise EmitError(f"selection {path} lists {duplicates} twice")
    return tuple(ids)


def render_yaml(document: Mapping[str, Any]) -> str:
    """The one YAML style every emitted bundle document is written in."""
    return yaml.safe_dump(
        dict(document), sort_keys=False, default_flow_style=False, width=100, allow_unicode=True
    )


def emit(candidate: CandidateBundle, registry_root: Path | str) -> Path:
    """Emit `candidate` into `<registry_root>/<store_id>/` and return that path."""
    release = _release_document(candidate)
    registry_root = Path(registry_root)
    staging_root = registry_root / STAGING_DIRNAME
    staging = staging_root / candidate.store_id
    target = registry_root / candidate.store_id
    with _emit_lock(staging_root, candidate.store_id):
        _put_back_an_interrupted_swap(staging, target)
        try:
            _stage(candidate, release, staging)
            staged = bundle.load(candidate.store_id, registry_root=staging_root)
            # Lineage resolves against the real registry: a parent is never staged.
            errors = generation_rules.check_with_contract(staged, registry_root=registry_root)
            if errors:
                raise EmitError(
                    f"{candidate.store_id} fails the Release Bundle contract or a Manifest Generation "
                    "rule; nothing was emitted:\n  - " + "\n  - ".join(errors)
                )
            index.write_bundle_summary(staged)
            # Checked last, under the lock, so a bundle accepted while this one
            # was staged is still refused.
            _refuse_replacing_more_than_a_candidate(target)
            _replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return target


def _increment_document(candidate: IncrementCandidate, number: int) -> dict[str, Any]:
    commands = list(candidate.commands)
    if not commands or not all(isinstance(c, str) and c.strip() for c in commands):
        raise EmitError(f"{candidate.store_id}: the commands that produced the increment are required")
    document: dict[str, Any] = {
        "increment": number,
        "store_id": candidate.store_id,
        "status": CANDIDATE_STATUS,
        "reason": candidate.reason,
        "created_at": candidate.created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": {
            **({"version": candidate.generator_version} if candidate.generator_version else {}),
            "commands": commands,
        },
    }
    if candidate.source_snapshot is not None:
        document["source_snapshot"] = dict(candidate.source_snapshot)
    if candidate.selection is not None:
        document["selection"] = {
            "path": candidate.selection_path,
            "sha256": candidate.selection_sha256,
            "n": len(candidate.selection),
        }
    if candidate.resolver is not None:
        document["resolver"] = dict(candidate.resolver)
    document.update({
        "require_added": list(candidate.require_added),
        "drops": [dict(d) for d in candidate.drops],
        "erratum": dict(candidate.erratum) if candidate.erratum else None,
        "acceptance": None,
        "accepted_warnings": [],
        "published": None,
    })
    return document


def _checksums(text: str) -> dict[str, str]:
    """`checksum` -> `analysis_id` for every row of an analyses table that states one."""
    rows = list(csv.DictReader(text.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE))
    return {r["checksum"]: r.get("analysis_id", "") for r in rows if (r.get("checksum") or "").strip()}


def _reconcile_selection(candidate: IncrementCandidate) -> None:
    """Every listed id is a row of the increment, admitted or refused, and every row is listed (ruling 5)."""
    if candidate.analyses_tsv is None:
        if candidate.selection is not None:
            raise EmitError(f"{candidate.store_id}: a drop-only increment takes no selection; its drops are listed")
        return
    if candidate.selection is None:
        raise EmitError(
            f"{candidate.store_id}: an increment that adds rows is reconciled to an explicit selection; give one"
        )
    rows = list(csv.DictReader(candidate.analyses_tsv.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE))
    ids = [r.get("analysis_id", "") for r in rows]
    listed = set(candidate.selection)
    extra = [a for a in ids if a not in listed]
    missing = [a for a in candidate.selection if a not in set(ids)]
    if extra:
        raise EmitError(f"{candidate.store_id}: increment rows {extra[:10]} are not listed in the selection")
    if missing:
        raise EmitError(
            f"{candidate.store_id}: {missing[:10]} are listed but not rows of the increment, neither admitted "
            "nor refused at resolution"
        )


def emit_increment(candidate: IncrementCandidate, registry_root: Path | str) -> Path:
    """Emit `candidate` as `<registry_root>/<id>/increments/<nnnn>/` and return that path (ADR 0045 §3).

    Nothing else in the bundle changes. Refused, with nothing written: a bundle
    that is not a validated Reference-Completed Hybrid, one with an open
    increment, a row whose checksum is a bundle row's, and anything the
    contract or Manifest Generation's rules refuse in the bundle with the
    increment staged into it.
    """
    store_id = candidate.store_id
    if not paths.is_valid_store_id(store_id):
        raise EmitError(f"{store_id!r} is not a Store id (OGS-NNNNN)")
    for name in candidate.sidecars:
        if not name or name != Path(name).name or name in (".", ".."):
            raise EmitError(f"sidecar name {name!r} must be a plain file name under sidecars/")
    _reconcile_selection(candidate)
    registry_root = Path(registry_root)
    target = registry_root / store_id
    staging_root = registry_root / STAGING_DIRNAME
    with _emit_lock(staging_root, store_id):
        if target.is_symlink() or not (target / "release.yaml").is_file():
            raise EmitError(f"{store_id} has no Release Bundle at {target} to take an increment")
        loaded = bundle.load(store_id, registry_root=registry_root)
        ineligible = increment.eligibility_errors(loaded)
        if ineligible:
            raise EmitError(f"{store_id}: " + "; ".join(ineligible))
        try:
            existing = increment.load(loaded)
        except increment.IncrementError as exc:
            raise EmitError(str(exc)) from exc
        opened = [i for i in existing if i.is_open]
        if opened:
            raise EmitError(
                f"{store_id} has an open increment, {increment.directory_name(opened[0].number)} "
                f"({opened[0].status}); it is published or withdrawn before another is emitted"
            )
        if candidate.analyses_tsv is not None:
            bundle_sums = _checksums(loaded.analyses_path.read_text(encoding="utf-8"))
            for checksum, analysis_id in _checksums(candidate.analyses_tsv).items():
                if checksum in bundle_sums:
                    raise EmitError(
                        f"{store_id}: increment row {analysis_id!r} has the checksum of bundle row "
                        f"{bundle_sums[checksum]!r}: the same file under another id"
                    )
        number = len(existing) + 1
        name = increment.directory_name(number)
        stage_parent = staging_root / f"{store_id}.increment-{name}"
        shutil.rmtree(stage_parent, ignore_errors=True)
        try:
            staged_bundle = stage_parent / store_id
            shutil.copytree(target, staged_bundle, symlinks=True)
            staged = staged_bundle / increment.INCREMENTS_DIRNAME / name
            staged.mkdir(parents=True)
            if candidate.analyses_tsv is not None:
                (staged / increment.ANALYSES_FILE).write_text(candidate.analyses_tsv, encoding="utf-8")
            if candidate.sidecars:
                (staged / increment.SIDECARS_DIRNAME).mkdir()
                for sidecar, text in candidate.sidecars.items():
                    (staged / increment.SIDECARS_DIRNAME / sidecar).write_text(text, encoding="utf-8")
            (staged / increment.INCREMENT_FILE).write_text(
                render_yaml(_increment_document(candidate, number)), encoding="utf-8"
            )
            staged_loaded = bundle.load(store_id, registry_root=stage_parent)
            errors = list(generation_rules.check_with_contract(staged_loaded, registry_root=registry_root))
            if candidate.analyses_tsv is not None:
                inc = increment.get(staged_loaded, number)
                errors += [f"increment {name}: {e}" for e in generation_rules.check(inc.as_bundle(staged_loaded))]
            if errors:
                raise EmitError(
                    f"{store_id}: increment {name} fails the Release Bundle contract or a Manifest Generation "
                    "rule; nothing was emitted:\n  - " + "\n  - ".join(errors)
                )
            (target / increment.INCREMENTS_DIRNAME).mkdir(exist_ok=True)
            destination = target / increment.INCREMENTS_DIRNAME / name
            if destination.exists() or destination.is_symlink():
                raise EmitError(f"{store_id}: {destination} already exists; nothing was emitted")
            os.rename(staged, destination)
        finally:
            shutil.rmtree(stage_parent, ignore_errors=True)
    return destination


def read_rendered_increment(
    directory: Path | str, store_id: str, commands: Sequence[str], selection: Path | str | None = None,
) -> IncrementCandidate:
    """Read an increment another renderer wrote: `increment.yaml`'s declaration, its rows and sidecars."""
    directory = Path(directory)
    allowed = {increment.INCREMENT_FILE, increment.ANALYSES_FILE, increment.SIDECARS_DIRNAME}
    unexpected = sorted(p.name for p in directory.iterdir() if p.name not in allowed or p.is_symlink())
    if unexpected:
        raise EmitError(f"{directory} holds what an Increment Bundle does not: {', '.join(unexpected)}")
    path = directory / increment.INCREMENT_FILE
    declaration = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    if not isinstance(declaration, Mapping):
        raise EmitError(f"{path} must be a YAML mapping declaring at least `reason`")
    for key in ("status", "acceptance", "published", "increment"):
        if declaration.get(key) not in (None, CANDIDATE_STATUS if key == "status" else None):
            raise EmitError(f"{path} may not set {key!r}: the emit numbers it and a human accepts it")
    sidecar_dir = directory / increment.SIDECARS_DIRNAME
    sidecars = sorted(sidecar_dir.iterdir()) if sidecar_dir.is_dir() else []
    analyses = directory / increment.ANALYSES_FILE
    generator = declaration.get("generator") if isinstance(declaration.get("generator"), Mapping) else {}
    return IncrementCandidate(
        store_id=store_id,
        reason=str(declaration.get("reason") or ""),
        commands=tuple(commands),
        analyses_tsv=analyses.read_text(encoding="utf-8") if analyses.is_file() else None,
        sidecars={p.name: p.read_text(encoding="utf-8") for p in sidecars if p.is_file()},
        generator_version=generator.get("version"),
        source_snapshot=declaration.get("source_snapshot"),
        resolver=declaration.get("resolver"),
        require_added=tuple(declaration.get("require_added") or ()),
        drops=tuple(declaration.get("drops") or ()),
        erratum=declaration.get("erratum"),
        created_at=declaration.get("created_at"),
        **selection_fields(selection),
    )


def selection_fields(selection: Path | str | None) -> dict[str, Any]:
    """The candidate fields of a roster file: its ids, path and sha256; none without one."""
    if selection is None:
        return {}
    # Manifest Generation's own streaming digest; imported here so the emitter stays cheap to load.
    from manifest_generation.candidate_workflow import sha256_file

    path = Path(selection).resolve()
    return {
        "selection": read_selection(path),
        "selection_path": str(path),
        "selection_sha256": sha256_file(path),
    }


def read_rendered(directory: Path | str, store_id: str, commands: Sequence[str]) -> CandidateBundle:
    """Read a bundle another renderer wrote into `directory` (the command line's `--from`).

    The YAML documents are read as data, so the emitter rewrites them in its
    own style. Anything a Release Bundle does not hold, or an entry of the
    wrong kind, is refused rather than silently left behind.
    """
    directory = Path(directory)
    unexpected = [
        p.name for p in directory.iterdir()
        if not (
            (p.name in RENDERED_FILES and p.is_file())
            or (p.name in RENDERED_DIRECTORIES and p.is_dir())
        )
    ]
    sidecar_dir = directory / "sidecars"
    sidecars = sorted(sidecar_dir.iterdir()) if sidecar_dir.is_dir() else []
    unexpected += [f"sidecars/{p.name}" for p in sidecars if not p.is_file()]
    map_dir = directory / "column_maps"
    column_maps = sorted(map_dir.iterdir()) if map_dir.is_dir() else []
    unexpected += [f"column_maps/{p.name}" for p in column_maps if not p.is_file() or p.is_symlink()]
    if unexpected:
        raise EmitError(
            f"{directory} holds what a Release Bundle does not, or of the wrong kind: {', '.join(sorted(unexpected))}"
        )

    def document(name: str) -> Mapping[str, Any] | None:
        path = directory / name
        if not path.is_file():
            return None
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise EmitError(f"{path} is not a YAML mapping")
        return loaded

    release, build = document("release.yaml"), document("build.yaml")
    if release is None or build is None or not (directory / "analyses.tsv").is_file():
        raise EmitError(f"{directory} needs release.yaml, build.yaml and analyses.tsv")
    return CandidateBundle(
        store_id=store_id,
        release=release,
        build=build,
        analyses_tsv=(directory / "analyses.tsv").read_text(encoding="utf-8"),
        commands=tuple(commands),
        validation=document("validation.yaml"),
        sidecars={p.name: p.read_text(encoding="utf-8") for p in sidecars},
        column_maps={p.name: p.read_bytes() for p in column_maps},
    )


def _release_document(candidate: CandidateBundle) -> dict[str, Any]:
    """The `release.yaml` to write, refusing input that could escape the bundle or misstate it.

    Runs before any path is built. A renderer may state `status: candidate`
    and the same commands; a different value is refused, never overwritten.
    """
    store_id = candidate.store_id
    if not paths.is_valid_store_id(store_id):
        raise EmitError(f"{store_id!r} is not a Store Release id (OGS-NNNNN)")
    for name in candidate.sidecars:
        if not name or name != Path(name).name or name in (".", ".."):
            raise EmitError(f"sidecar name {name!r} must be a plain file name under sidecars/")
    for name in candidate.column_maps:
        if not bundle.is_valid_column_map_file(name):
            raise EmitError(
                f"Column Map file name {name!r} must be a plain file name under column_maps/ "
                f"matching {bundle.INDEXED_SUBSET_NAME_RE.pattern}, with no '..', ending "
                f"{bundle.COLUMN_MAP_SUFFIX}"
            )
    commands = list(candidate.commands)
    if not commands or not all(isinstance(c, str) and c.strip() for c in commands):
        raise EmitError(f"{store_id}: the generator commands that produced it are required")

    release = dict(candidate.release)
    if release.get("status", CANDIDATE_STATUS) != CANDIDATE_STATUS:
        raise EmitError(
            f"{store_id}: the rendered release.yaml says Release Status {release['status']!r}; "
            "only a candidate is emitted, and a human accepts it"
        )
    generator = dict(release.get("generator") or {})
    if generator.get("commands", commands) != commands:
        raise EmitError(
            f"{store_id}: the rendered release.yaml records generator commands "
            f"{generator['commands']!r}, not the ones given ({commands!r})"
        )
    release["status"] = CANDIDATE_STATUS
    release["generator"] = {**generator, "commands": commands}
    return release


@contextmanager
def _emit_lock(staging_root: Path, store_id: str) -> Iterator[None]:
    """Hold `<staging_root>/<id>.lock`, so two emits never share a staging directory."""
    staging_root.mkdir(parents=True, exist_ok=True)
    fd = os.open(staging_root / f"{store_id}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EmitError(f"an emit of {store_id} is in progress; nothing was emitted") from None
        yield
    finally:
        os.close(fd)


def _previous(staging: Path) -> Path:
    """Where the fallback swap stashes the candidate it replaces."""
    return staging.with_name(staging.name + ".previous")


def _put_back_an_interrupted_swap(staging: Path, target: Path) -> None:
    """Finish what a fallback swap killed between its two moves left behind.

    With `stores/<id>/` missing, the stash is the only copy of the earlier
    candidate, so it is put back. With `stores/<id>/` present, the swap
    completed and the stash is stale.
    """
    previous = _previous(staging)
    if not previous.exists():
        return
    if target.exists() or target.is_symlink():
        shutil.rmtree(previous)
    else:
        os.replace(previous, target)


def _refuse_replacing_more_than_a_candidate(target: Path) -> None:
    """Only a candidate may be replaced: anything past it is a Store Release's frozen input."""
    if not (target.exists() or target.is_symlink()):
        return
    if target.is_symlink() or not (target / "release.yaml").is_file():
        raise EmitError(f"refusing to emit over {target}: it is not a Release Bundle")
    status = bundle.load(target.name, registry_root=target.parent).status
    if status != CANDIDATE_STATUS:
        raise EmitError(
            f"refusing to emit over {target.name}: its release.yaml gives Release Status {status!r}, "
            "and only a candidate may be replaced. A new candidate needs a new Store Release id."
        )


def _stage(candidate: CandidateBundle, release: Mapping[str, Any], staging: Path) -> None:
    """Write every file of the bundle under `staging`, `release.yaml` last."""
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    (staging / "analyses.tsv").write_text(candidate.analyses_tsv, encoding="utf-8")
    if candidate.sidecars:
        (staging / "sidecars").mkdir()
        for name, text in candidate.sidecars.items():
            (staging / "sidecars" / name).write_text(text, encoding="utf-8")
    if candidate.column_maps:
        (staging / "column_maps").mkdir()
        for name, data in candidate.column_maps.items():
            (staging / "column_maps" / name).write_bytes(data)
    (staging / "build.yaml").write_text(render_yaml(candidate.build), encoding="utf-8")
    if candidate.validation is not None:
        (staging / "validation.yaml").write_text(render_yaml(candidate.validation), encoding="utf-8")
    (staging / "release.yaml").write_text(render_yaml(release), encoding="utf-8")


def _replace(staging: Path, target: Path) -> None:
    """Put the staged bundle at `target` in one step.

    A new bundle is one rename. Over an existing candidate, the two
    directories are exchanged with Linux `renameat2(RENAME_EXCHANGE)`, so
    `stores/<id>/` always holds one whole candidate, and the earlier one ends
    up in `staging` to be removed. Where the filesystem cannot exchange (NFS,
    for one), the earlier candidate is stashed first and put back if the
    second move fails. The stash is removed only once the new bundle is in
    place; a crash before then leaves it for the next emit to put back.
    """
    if not target.exists():
        os.replace(staging, target)
        return
    if _exchange(staging, target):
        return
    previous = _previous(staging)
    os.replace(target, previous)
    try:
        os.replace(staging, target)
    except OSError:
        os.replace(previous, target)
        raise
    shutil.rmtree(previous)


# From <linux/fcntl.h> and renameat2(2): resolve both paths against the
# working directory, and exchange them rather than overwrite.
_AT_FDCWD = -100
_RENAME_EXCHANGE = 2


def _exchange(a: Path, b: Path) -> bool:
    """Swap two paths atomically; False where the platform or filesystem cannot.

    ENOSYS is a kernel without renameat2, EINVAL a filesystem without the
    exchange flag, and EOPNOTSUPP a filesystem that refuses it: each means
    "use the fallback", not that anything went wrong.
    """
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        return False
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if renameat2(_AT_FDCWD, os.fsencode(a), _AT_FDCWD, os.fsencode(b), _RENAME_EXCHANGE) == 0:
        return True
    code = ctypes.get_errno()
    if code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        return False
    raise OSError(code, os.strerror(code), str(a), None, str(b))


def main(argv: Sequence[str] | None = None) -> int:
    return _emit_main(list(sys.argv[1:] if argv is None else argv))


def _emit_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id", help="the candidate's Store Release id, e.g. OGS-00042")
    parser.add_argument("--from", dest="rendered", help="directory holding the rendered bundle or increment")
    parser.add_argument(
        "--command", dest="commands", action="append",
        help="a generator command that produced it, in order (repeat for each)",
    )
    parser.add_argument("--registry-root", default=str(REPO_ROOT / "stores"))
    increment_options = parser.add_argument_group("increment mode (ADR 0045)")
    increment_options.add_argument("--increment", action="store_true",
                                   help="emit an Increment Bundle into a validated bundle")
    increment_options.add_argument("--selection", help="the roster file the rendered rows are reconciled to")
    increment_options.add_argument("--drop", dest="drops", action="append", default=[], metavar="ID=REASON",
                                   help="an Analysis to drop, with its reason (repeat for each)")
    increment_options.add_argument("--reason", help="why the increment is made")
    increment_options.add_argument("--defect", action="store_true", help="the drops remove defects: a Release Erratum")
    increment_options.add_argument("--erratum-issue", help="the issue the erratum cites")
    increment_options.add_argument("--erratum-text", help="the erratum's text")
    args = parser.parse_args(argv)
    if args.increment:
        return _emit_increment_main(args, argv)
    if not args.rendered or not args.commands:
        parser.error("--from and --command are required, except for a drop-only --increment")
    try:
        emitted = emit(read_rendered(args.rendered, args.store_id, args.commands), args.registry_root)
    except EmitError as exc:
        print(f"emit: {exc}", file=sys.stderr)
        return 1
    print(f"emit: {args.store_id} emitted to {emitted} with Release Status candidate")
    return 0


def _emit_increment_main(args: argparse.Namespace, argv: Sequence[str]) -> int:
    commands = args.commands or [" ".join(["python3", "-m", "manifest_generation.emit", *argv])]
    try:
        if args.rendered:
            if args.drops:
                raise EmitError("--drop is for a drop-only increment; declare drops in the rendered increment.yaml")
            if not args.selection:
                raise EmitError("--increment --from needs --selection, the roster its rows are reconciled to")
            candidate = read_rendered_increment(args.rendered, args.store_id, commands, args.selection)
        else:
            drops = []
            for item in args.drops:
                analysis_id, separator, reason = item.partition("=")
                if not separator or not analysis_id or not reason.strip():
                    raise EmitError(f"--drop {item!r} must be ID=REASON")
                drops.append({"analysis_id": analysis_id, "reason": reason, "defect": bool(args.defect)})
            if not drops:
                raise EmitError("an increment needs --from (rows to add) or at least one --drop")
            if not args.reason:
                raise EmitError("a drop-only increment needs --reason")
            erratum = None
            if args.erratum_issue or args.erratum_text:
                erratum = {"issue": args.erratum_issue or "", "text": args.erratum_text or ""}
            candidate = IncrementCandidate(
                store_id=args.store_id, reason=args.reason, commands=tuple(commands),
                drops=tuple(drops), erratum=erratum,
            )
        emitted = emit_increment(candidate, args.registry_root)
    except (EmitError, increment.IncrementError, OSError) as exc:
        print(f"emit: {exc}", file=sys.stderr)
        return 1
    print(f"emit: {args.store_id} increment emitted to {emitted} as a candidate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
