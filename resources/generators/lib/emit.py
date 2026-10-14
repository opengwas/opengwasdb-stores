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

Python generators call `emit()`. R generators render the files into a
directory and call the command line:

    python3 resources/generators/lib/emit.py OGS-00042 --from <dir> \\
        --command "Rscript ... --mode=emit" [--command ...] [--registry-root stores]

and ask it where their working files go, `<artifact-root>/<id>/generate`,
resolved as Store Release Build resolves the artifact root:

    python3 resources/generators/lib/emit.py work-root OGS-00042 [--artifact-root <path>]

A legacy generator keeps a family-shaped working bundle there, and `project`
emits the current contract from it and its config's `build:` block:

    python3 resources/generators/lib/emit.py project --config <cfg.yaml> \\
        --generator-version sha256:... --command ... [--source-snapshot <yaml>]
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[3]

from ogstores import bundle, index, paths  # noqa: E402
from resources.generators.lib import generation_rules  # noqa: E402
from resources.generators.lib.indexed_subsets import IndexedSubsetConfigError, resolve_indexed_subsets  # noqa: E402

#: The registry subdirectory candidates are staged under. Hidden, so
#: `bundle-check` and `index` never see a half-written bundle, and ignored by
#: git, which keeps the per-id lock files out of the working tree's changes.
STAGING_DIRNAME: str = ".staging"
CANDIDATE_STATUS: str = "candidate"

#: What a legacy generator config's `build:` block must state to be projected.
BUILD_BLOCK_KEYS: tuple[str, ...] = ("layout", "completion_state", "command", "post")

#: The files a bundle directory may hold, for the command line's `--from`.
RENDERED_FILES: frozenset[str] = frozenset({"release.yaml", "build.yaml", "analyses.tsv", "validation.yaml"})


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
        if not ((p.name in RENDERED_FILES and p.is_file()) or (p.name == "sidecars" and p.is_dir()))
    ]
    sidecar_dir = directory / "sidecars"
    sidecars = sorted(sidecar_dir.iterdir()) if sidecar_dir.is_dir() else []
    unexpected += [f"sidecars/{p.name}" for p in sidecars if not p.is_file()]
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
    )


def project_working_bundle(
    config: Mapping[str, Any],
    working_dir: Path | str,
    *,
    generator_version: str,
    commands: Sequence[str],
    created_at: str | None = None,
    source_snapshot: Mapping[str, Any] | None = None,
    repo_root: Path = REPO_ROOT,
) -> CandidateBundle:
    """The current-contract candidate a legacy generator's working bundle projects to (#248).

    `analyses.tsv`, the flat `sidecars/` and `validation.yaml` come from the
    working bundle; `release.yaml` and `build.yaml` come from the config,
    whose `build:` block is the Build Recipe. Nothing a bundle states is
    guessed: the config must give the label, access posture and recipe. The
    working bundle's own family-shaped `release.yaml` and `build.yaml` are not
    carried over, but the command log its `release.yaml` records is: the
    candidate records those commands, then `commands`.
    """
    working_dir = Path(working_dir)
    store_id = str((config.get("output") or {}).get("store_id") or "")
    build_config = config.get("build")
    missing = [
        key for key in BUILD_BLOCK_KEYS
        if not isinstance(build_config, Mapping) or build_config.get(key) in (None, "")
    ] + [key for key in ("label", "access_posture") if not config.get(key)]
    if missing:
        raise EmitError(
            f"{store_id}: the config must state {', '.join(missing)} "
            "(a build: block with layout, completion_state, command and post; label; access_posture)"
        )
    if not (working_dir / "analyses.tsv").is_file():
        raise EmitError(f"no working analyses.tsv under {working_dir}; run the generator's emit mode first")
    created_at = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    layout = build_config.get("layout")
    try:
        subsets, opt_out = resolve_indexed_subsets(config, str(layout), repo_root)
    except IndexedSubsetConfigError as exc:
        raise EmitError(f"{store_id}: {exc}") from exc

    build: dict[str, Any] = {
        "store_id": store_id,
        "layout": layout,
        "completion_state": build_config.get("completion_state"),
        "build": {"command": build_config["command"], "options": dict(build_config.get("options") or {})},
        "post": dict(build_config.get("post") or {}),
    }
    if subsets is not None:
        build["indexed_subsets"] = subsets
    notes = str(config.get("notes") or "")
    if opt_out:
        notes += ("\n\n" if notes else "") + (
            "Indexed Variant Subsets (#210): the default hm3 Indexed Variant Subset is "
            f"intentionally omitted: {opt_out}"
        )
    release: dict[str, Any] = {
        "store_id": store_id,
        "label": config["label"],
        "access_posture": config["access_posture"],
        "status": CANDIDATE_STATUS,
        "derived_from": None,
        "created_at": created_at,
        "source_snapshot_id": (config.get("source") or {}).get("source_snapshot_id"),
    }
    if source_snapshot is not None:
        release["source_snapshot"] = dict(source_snapshot)
    release["generator"] = {"version": generator_version}
    release["description"] = config.get("description")
    release["notes"] = notes

    validation = None
    if (working_dir / "validation.yaml").is_file():
        validation = dict(yaml.safe_load((working_dir / "validation.yaml").read_text(encoding="utf-8")) or {})
        validation["validated_at"] = created_at
    sidecar_dir = working_dir / "sidecars"
    sidecars = (
        {p.name: p.read_text(encoding="utf-8") for p in sorted(sidecar_dir.iterdir()) if p.is_file()}
        if sidecar_dir.is_dir() else {}
    )
    return CandidateBundle(
        store_id=store_id,
        release=release,
        build=build,
        analyses_tsv=(working_dir / "analyses.tsv").read_text(encoding="utf-8"),
        commands=tuple(dict.fromkeys([*_working_command_log(working_dir), *commands])),
        validation=validation,
        sidecars=sidecars,
    )


def _working_command_log(working_dir: Path) -> list[str]:
    """The commands a working bundle's own `release.yaml` records as having produced it."""
    path = working_dir / "release.yaml"
    if not path.is_file():
        return []
    generator = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("generator") or {}
    logged = generator.get("commands") or generator.get("command") or []
    return [str(c) for c in ([logged] if isinstance(logged, str) else logged) if str(c).strip()]


def work_root(store_id: str, artifact_root: Path | str | None = None) -> Path:
    """Where a generator keeps its working files: `<artifact-root>/<id>/generate`."""
    if not paths.is_valid_store_id(store_id):
        raise EmitError(f"{store_id!r} is not a Store Release id (OGS-NNNNN)")
    return paths.generation_dir(store_id, paths.artifact_root(artifact_root))


def emit_working_bundle(
    config: Mapping[str, Any],
    *,
    generator_version: str,
    commands: Sequence[str],
    source_snapshot: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> Path:
    """Project a legacy generator's working bundle from its work root and emit it (#248).

    `config` is the generator's own: `output.store_id` names the release, the
    `build:` block is its Build Recipe, and `output.registry_root` (relative to
    the repository, default `stores`) is where it lands.
    """
    output = config.get("output") or {}
    store_id = str(output.get("store_id") or "")
    candidate = project_working_bundle(
        config,
        work_root(store_id),
        generator_version=generator_version,
        commands=commands,
        source_snapshot=source_snapshot,
        created_at=created_at,
    )
    registry_root = Path(output.get("registry_root") or "stores")
    return emit(candidate, registry_root if registry_root.is_absolute() else REPO_ROOT / registry_root)


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
    words = list(sys.argv[1:] if argv is None else argv)
    if words[:1] == ["work-root"]:
        return _work_root_main(words[1:])
    if words[:1] == ["project"]:
        return _project_main(words[1:])
    return _emit_main(words)


def _project_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="emit.py project", description="emit a legacy generator's working bundle")
    parser.add_argument("--config", required=True, help="the generator config, with output.store_id and build:")
    parser.add_argument("--generator-version", required=True)
    parser.add_argument("--command", dest="commands", action="append", required=True)
    parser.add_argument("--source-snapshot", help="YAML mapping recorded as release.yaml source_snapshot")
    parser.add_argument("--created-at", help="pin created_at (UTC, e.g. 2026-10-14T00:00:00Z) for a reproducible emit")
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    snapshot = yaml.safe_load(Path(args.source_snapshot).read_text(encoding="utf-8")) if args.source_snapshot else None
    try:
        emitted = emit_working_bundle(
            config,
            generator_version=args.generator_version,
            commands=args.commands,
            source_snapshot=snapshot,
            created_at=args.created_at,
        )
    except EmitError as exc:
        print(f"emit: {exc}", file=sys.stderr)
        return 1
    print(f"emit: {emitted.name} emitted to {emitted} with Release Status candidate")
    return 0


def _work_root_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="emit.py work-root", description="print a generator's work root")
    parser.add_argument("store_id")
    parser.add_argument("--artifact-root", help="overrides OPENGWASDB_ARTIFACT_ROOT and ogstores.yaml")
    args = parser.parse_args(argv)
    try:
        print(work_root(args.store_id, args.artifact_root))
    except EmitError as exc:
        print(f"emit: {exc}", file=sys.stderr)
        return 1
    return 0


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
