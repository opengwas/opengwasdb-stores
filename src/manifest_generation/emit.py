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
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import os
import shutil
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ogstores import bundle, index, paths
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
    parser.add_argument("--from", dest="rendered", required=True, help="directory holding the rendered bundle")
    parser.add_argument(
        "--command", dest="commands", action="append", required=True,
        help="a generator command that produced it, in order (repeat for each)",
    )
    parser.add_argument("--registry-root", default=str(REPO_ROOT / "stores"))
    args = parser.parse_args(argv)
    try:
        emitted = emit(read_rendered(args.rendered, args.store_id, args.commands), args.registry_root)
    except EmitError as exc:
        print(f"emit: {exc}", file=sys.stderr)
        return 1
    print(f"emit: {args.store_id} emitted to {emitted} with Release Status candidate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
