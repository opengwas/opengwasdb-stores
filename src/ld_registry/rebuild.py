"""Rebuild an LD Store or Genotype Store from its record, into a new directory (#541).

The record's `rebuild.commands` are run with only the output changed: the
Store's directory, the parent of its `location`, becomes the new directory, in
every argument that lies under it. Inputs the commands read from inside that
directory are a Genotype Store's staged copies (its sidecar pins them by a
path relative to itself), so they are copied first, modification times kept,
and the new sidecar pins them as the original does. So a Genotype Store can be
rebuilt only while its original's directory exists. A record that validates
nothing is validated once after its build, at the build's threads; one that
does is not validated again. The commands run in build92.sh's environment
(`recipe.run_in_environment`).

A command is run only when it is the engine's: `opengwasdb ld build` or
`opengwasdb ld validate`, optionally after `pixi run -e <env>`. `ogld <sub>`,
the engine's former name, is run as `opengwasdb ld <sub>` at the engine the
lock pins. Anything else, such as OGL-00001's `scripts/build_hgdp1kgp.py build`,
is not runnable, and the record is refused. The rebuilt Store is a new Store:
nothing here writes a record, and nothing writes inside a recorded Store's
location.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ld_registry import record
from ld_registry.recipe import CommandRunner, Copier, run_command, run_in_environment, stage_copy

ENGINE_SUBCOMMANDS = ("build", "validate")
#: Shell syntax a recorded command must not hold: each command runs as one argv, never in a shell.
SHELL_TOKENS = frozenset({"&&", "||", ";", "|", "&", ">", ">>", "<", "2>", "2>&1"})


class RebuildRefusal(ValueError):
    """The record cannot be rebuilt here; the message names the record and why."""


@dataclass(frozen=True)
class Rebuild:
    """A rebuild: the new Store, its inputs to copy, the record's commands rewritten, and its validation."""

    store: Path
    copies: tuple[tuple[Path, Path], ...]
    #: The record's commands rewritten, and its validation when it recorded none.
    commands: tuple[tuple[str, ...], ...]
    #: The recorded Store, and the digest its record holds of its provenance.
    original: Path
    recorded_sha256: str | None

    @property
    def validate(self) -> tuple[str, ...]:
        """The command among `commands` that validates the rebuilt Store."""
        return next(argv for argv in self.commands if argv[2] == "validate")

    @property
    def keep(self) -> Path | None:
        """The keep file the build reads, so the acceptance can hold n to it."""
        build = next(argv for argv in self.commands if argv[2] == "build")
        keeps = [value for flag, value in zip(build, build[1:]) if flag == "--keep"]
        return Path(keeps[-1]) if keeps else None


def engine_argv(command: str) -> list[str] | None:
    """The engine argv a recorded command runs, or None when it is not `opengwasdb ld build|validate`."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if argv[:2] == ["pixi", "run"]:
        argv = argv[2:]
        if argv[:1] in (["-e"], ["--environment"]):
            argv = argv[2:]
    if argv[:1] == ["ogld"]:
        argv = ["opengwasdb", "ld", *argv[1:]]
    if argv[:2] != ["opengwasdb", "ld"] or len(argv) < 4 or argv[2] not in ENGINE_SUBCOMMANDS:
        return None
    return argv


def protected_locations(ld_root: Path) -> list[Path]:
    """Every recorded Store's location, and a Genotype Store's directory, which its pins resolve into."""
    protected: list[Path] = []
    if not Path(ld_root).is_dir():
        return protected
    for entry in sorted(Path(ld_root).iterdir()):
        document, _ = record.read_document(record.record_path(Path(ld_root), entry.name))
        location = document.get("location") if document else None
        if isinstance(location, str) and location:
            protected.append(Path(location))
            if document.get("kind") == "genotype_store":
                protected.append(Path(location).parent)
    return protected


def _inside(path: Path, parent: Path) -> bool:
    """Whether `path` is `parent` or under it, once each link in either is followed."""
    path, parent = Path(path).resolve(), Path(parent).resolve()
    return path == parent or path.is_relative_to(parent)


def plan_rebuild(document: dict[str, Any], out: Path, protected: Iterable[Path] = ()) -> Rebuild:
    """The rebuild of `document`'s Store into the directory `out`, or a refusal naming why.

    It writes nothing, and reads the disk only to follow links when it compares
    `out` with the recorded Stores; `rebuild_store` refuses an `out` that exists.
    """
    ld_id = document.get("ld_id", "the record")
    out = Path(out).absolute()
    for location in protected:
        if _inside(out, Path(location)):
            raise RebuildRefusal(f"{ld_id}: {out} is inside {location}, a recorded Store's; a rebuild is a new Store")
    location = document.get("location")
    if not isinstance(location, str) or not Path(location).is_absolute():
        raise RebuildRefusal(f"{ld_id}: location: the record names no Store to rebuild")
    errors = [e for e in record.validate(document, Path(ld_id, record.RECORD_FILENAME)) if e.field.startswith("rebuild")]
    if errors or not isinstance(document.get("rebuild"), dict):
        problems = "; ".join(f"{e.field} {e.message}" for e in errors) or "rebuild is required"
        raise RebuildRefusal(f"{ld_id}: the record has no runnable rebuild: {problems}")

    commands: list[list[str]] = []
    for number, command in enumerate(document["rebuild"]["commands"], start=1):
        argv = engine_argv(command)
        if argv is None:
            raise RebuildRefusal(
                f"{ld_id}: rebuild.commands[{number}] is not an `opengwasdb ld build` or `validate` command, so the "
                f"record has no runnable rebuild: {command}"
            )
        shell = sorted(SHELL_TOKENS.intersection(argv))
        if shell:
            raise RebuildRefusal(f"{ld_id}: rebuild.commands[{number}] holds shell syntax ({' '.join(shell)}): {command}")
        commands.append(argv)
    builds = [argv for argv in commands if argv[2] == "build"]
    if [argv[3] for argv in builds] != [location]:
        written = ", ".join(argv[3] for argv in builds) or "no Store"
        raise RebuildRefusal(f"{ld_id}: rebuild.commands builds {written}; a runnable rebuild builds {location} once")

    old = Path(location).parent
    store = out / Path(location).name

    def moved(argument: str) -> str:
        path = Path(argument)
        return str(out / path.relative_to(old)) if path.is_absolute() and path.is_relative_to(old) else argument

    # The inputs read from inside the Store's directory, in the order the commands name them;
    # a `--pgen` names its fileset, so its psam and pvar come with it.
    copies: dict[Path, Path] = {}
    for argv in commands:
        for flag, value in zip(argv, argv[1:]):
            path = Path(value)
            if not (path.is_absolute() and path.is_relative_to(old)) or _inside(path, Path(location)):
                continue
            sources = [path.with_suffix(f".{ext}") for ext in ("pgen", "psam", "pvar")] if flag == "--pgen" else [path]
            copies.update((source, Path(moved(str(source)))) for source in sources)
    rewritten = tuple(tuple(moved(argument) for argument in argv) for argv in commands)
    if not any(argv[2] == "validate" for argv in rewritten):
        build = next(argv for argv in rewritten if argv[2] == "build")
        threads = [value for flag, value in zip(build, build[1:]) if flag == "--threads"]
        rewritten += (("opengwasdb", "ld", "validate", *(("--threads", threads[-1]) if threads else ()), str(store)),)
    digest = document.get("provenance_sha256")
    return Rebuild(store, tuple(copies.items()), rewritten, Path(location), digest if isinstance(digest, str) else None)


def rebuild_store(
    ld_root: Path,
    ld_id: str,
    out: Path,
    *,
    run: CommandRunner = run_command,
    copy: Copier = stage_copy,
) -> Rebuild:
    """Rebuild `ld_id`'s Store into `out` from its record's `rebuild`. The record is only read."""
    path = record.record_path(Path(ld_root), ld_id)
    document, errors = record.read_document(path)
    if document is None:
        raise RebuildRefusal("; ".join(str(error) for error in errors))
    plan = plan_rebuild(document, out, protected_locations(Path(ld_root)))
    if Path(out).exists():
        raise RebuildRefusal(f"{ld_id}: {Path(out).absolute()} exists; a rebuild writes a new directory")
    for source, target in plan.copies:
        copy(source, target)
    run_in_environment(plan.commands, plan.store, run)
    return plan
