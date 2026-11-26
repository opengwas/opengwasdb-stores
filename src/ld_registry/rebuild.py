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

A command is run only when it is the engine's: `opengwasdb ld build`,
`opengwasdb ld import` or `opengwasdb ld validate`, optionally after
`pixi run -e <env>`. `ogld <sub>`, the engine's former name, is run as
`opengwasdb ld <sub>` at the engine the lock pins. Anything else, such as
OGL-00001's `scripts/build_hgdp1kgp.py build`, is not runnable, and the record
is refused. An import's `--work` is its output too: it moves with the Store and
is never copied (#542). The rebuilt Store is a new Store: nothing here writes a
record, and nothing writes inside a recorded Store's location.

A plan-only rebuild (#542) runs an import's commands alone, each with
`--plan-only`, after the engine's own help shows it has the option: it writes
the plan and lifted tables in the new work directory, and no Store.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ld_registry import record
from ld_registry.argv import options, rewrite_value, values
from ld_registry.recipe import (
    CommandRunner,
    Copier,
    require_plan_only,
    run_command,
    run_in_environment,
    stage_copy,
    validate_command,
)
from ogstores.store_info import Runner

ENGINE_SUBCOMMANDS = ("build", "import", "validate")
#: The subcommands that make a Store: a record's commands make its `location` once.
BUILDERS = ("build", "import")
#: Paths a command writes, never an input to copy: an import's work directory.
OUTPUT_FLAGS = frozenset({"--work"})
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

    def _builders(self, flag: str) -> Path | None:
        """The path the build or import gives `flag`, its last when it gives several."""
        build = next(argv for argv in self.commands if argv[2] in BUILDERS)
        given = values(build, flag)
        return Path(given[-1]) if given else None

    @property
    def keep(self) -> Path | None:
        """The keep file the build reads, so the acceptance can hold n to it; an import reads none."""
        return self._builders("--keep")

    @property
    def work(self) -> Path | None:
        """An import's work directory, where its plan is written; a build has none."""
        return self._builders("--work")


def engine_argv(command: str) -> list[str] | None:
    """The engine argv a recorded command runs, or None when it is not `opengwasdb ld build|import|validate`."""
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
    """Every recorded Store's location, and a Genotype Store's directory, which its pins resolve into.

    Every record read counts, valid or not (`record.load_records`). A record
    that cannot be read at all could hold any Store, so it is refused, never
    skipped: no Store is left unprotected.
    """
    documents, errors = record.load_records(Path(ld_root), invalid=True)
    unread = record.unread(errors)
    if unread:
        raise RebuildRefusal(
            "a record that cannot be read could name the Store a rebuild would write into: "
            + "; ".join(str(error) for error in unread)
        )
    protected: list[Path] = []
    for document in documents:
        location = document.get("location")
        if isinstance(location, str) and location:
            protected.append(Path(location))
            if document.get("kind") == "genotype_store":
                protected.append(Path(location).parent)
    return protected


def _inside(path: Path, parent: Path) -> bool:
    """Whether `path` is `parent` or under it, once each link in either is followed."""
    path, parent = Path(path).resolve(), Path(parent).resolve()
    return path == parent or path.is_relative_to(parent)


def plan_rebuild(
    document: dict[str, Any], out: Path, protected: Iterable[Path] = (), *, plan_only: bool = False
) -> Rebuild:
    """The rebuild of `document`'s Store into the directory `out`, or a refusal naming why.

    It writes nothing, and reads the disk only to follow links when it compares
    `out` with the recorded Stores; `rebuild_store` refuses an `out` that exists.
    With `plan_only`, the record's import alone, with `--plan-only`.
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
                f"{ld_id}: rebuild.commands[{number}] is not an `opengwasdb ld build`, `import` or `validate` "
                f"command, so the record has no runnable rebuild: {command}"
            )
        shell = sorted(SHELL_TOKENS.intersection(argv))
        if shell:
            raise RebuildRefusal(f"{ld_id}: rebuild.commands[{number}] holds shell syntax ({' '.join(shell)}): {command}")
        commands.append(argv)
    builds = [argv for argv in commands if argv[2] in BUILDERS]
    if [argv[3] for argv in builds] != [location]:
        written = ", ".join(argv[3] for argv in builds) or "no Store"
        raise RebuildRefusal(f"{ld_id}: rebuild.commands builds {written}; a runnable rebuild builds {location} once")

    old = Path(location).parent
    store = out / Path(location).name
    # An import's work directory is an output that moves with the Store: one elsewhere would not move,
    # and the rebuild would write into the original's.
    for work in (value for argv in builds for value in values(argv, "--work")):
        if not (Path(work).is_absolute() and Path(work).is_relative_to(old)):
            raise RebuildRefusal(
                f"{ld_id}: its import's --work {work} is not under {old}, the Store's directory, so it cannot move "
                "with the Store; a rebuild would write into the original work directory"
            )

    def moved_path(value: str) -> str:
        path = Path(value)
        return str(out / path.relative_to(old)) if path.is_absolute() and path.is_relative_to(old) else value

    def moved(argument: str) -> str:
        return rewrite_value(argument, moved_path)

    # The inputs read from inside the Store's directory, in the order the commands name them;
    # a `--pgen` names its fileset, so its psam and pvar come with it.
    copies: dict[Path, Path] = {}
    for argv in commands:
        for flag, value in options(argv):
            path = Path(value)
            if flag in OUTPUT_FLAGS or not (path.is_absolute() and path.is_relative_to(old)) or _inside(
                path, Path(location)
            ):
                continue
            sources = [path.with_suffix(f".{ext}") for ext in ("pgen", "psam", "pvar")] if flag == "--pgen" else [path]
            copies.update((source, Path(moved_path(str(source)))) for source in sources)
    rewritten = tuple(tuple(moved(argument) for argument in argv) for argv in commands)
    digest = document.get("provenance_sha256")
    recorded = digest if isinstance(digest, str) else None
    if plan_only:
        if builds[0][2] != "import":
            raise RebuildRefusal(f"{ld_id}: --plan-only re-plans an import, but its rebuild runs `opengwasdb ld build`")
        planned = tuple((*argv, "--plan-only") for argv in rewritten if argv[2] == "import")
        return Rebuild(store, tuple(copies.items()), planned, Path(location), recorded)
    if not any(argv[2] == "validate" for argv in rewritten):
        build = next(argv for argv in rewritten if argv[2] in BUILDERS)
        threads = values(build, "--threads")
        rewritten += (validate_command(store, threads[-1] if threads else None),)
    return Rebuild(store, tuple(copies.items()), rewritten, Path(location), recorded)


def rebuild_store(
    ld_root: Path,
    ld_id: str,
    out: Path,
    *,
    run: CommandRunner = run_command,
    copy: Copier = stage_copy,
    plan_only: bool = False,
    help_runner: Runner | None = None,
) -> Rebuild:
    """Rebuild `ld_id`'s Store into `out` from its record's `rebuild`, or only re-plan its import.

    The record is only read.
    """
    path = record.record_path(Path(ld_root), ld_id)
    document, errors = record.read_document(path)
    if document is None:
        raise RebuildRefusal("; ".join(str(error) for error in errors))
    plan = plan_rebuild(document, out, protected_locations(Path(ld_root)), plan_only=plan_only)
    if Path(out).exists():
        raise RebuildRefusal(f"{ld_id}: {Path(out).absolute()} exists; a rebuild writes a new directory")
    if plan_only:
        require_plan_only(help_runner)
    for source, target in plan.copies:
        copy(source, target)
    run_in_environment(plan.commands, plan.store, run)
    return plan
