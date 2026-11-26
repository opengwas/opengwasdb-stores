"""The HGDP+1kGP LD Store and Genotype Store recipe (#541; docs/spec/ld-store-registry.md, "The recipe").

From a panel's converted inputs (one pgen fileset per chromosome, its keep
file and its PC File), it builds an LD Store, or with `--genotypes` a Genotype
Store, through the engine's command line alone (ADR 0023):

1. a Genotype Store's inputs are staged first, as `build92.sh` (ogld#92) did:
   read-only copies, modification times kept, in `pgen/` and `inputs/` beside
   the sidecar, because a sidecar pins its pgens by a path relative to itself,
   each checked against the published LD Store's provenance (`source.sha256`,
   and `adjustment.pc_file.sha256` for the PC File);
2. `opengwasdb ld build`, with the flags today's Stores were built with, and
   Partner Rows at `--t-partner` 0.01, which the engine now requires;
3. `opengwasdb ld validate`;
4. the record writer, recording these commands as the Store's `rebuild`.

Each engine command runs in build92.sh's environment: one BLAS thread, no
user site, and a `TMPDIR` beside the Store that a successful build removes.

The plink2 conversion that makes the inputs stays in opengwasdb
(`scripts/ld/build_hgdp1kgp.py convert`, build-inputs §3).
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from ld_registry import REPO_ROOT, record
from ld_registry.argv import options as argv_options
from ld_registry.argv import values as argv_values
from ld_registry.ids import require_valid_ld_id
from ld_registry.provenance import ld_info
from ld_registry.writer import Written, write_record
from ogstores.store_info import Runner, run_engine
from ogstores.store_size import store_size_on_disk

REPOSITORY = "opengwas/opengwasdb-stores"

PANELS: tuple[str, ...] = ("AFR", "AMR", "EAS", "EUR", "SAS")
CHROMS: tuple[int, ...] = tuple(range(1, 23))

#: The flags the HGDP+1kGP Stores were built with: the ten Stores' provenance
#: (k 10 of target 10, k_min 5, mac_min 20, info_min null, r_scale 511) and
#: build92.sh's command line. `--t-partner` is the owner's 0.01 for these panels
#: (opengwasdb ADR 0097); the engine has no default for it or for `--r-scale`.
#: The Genotype Stores were built with it; the LD Stores OGL-00001 to 00005
#: were not (no Partner Rows), so an LD Store this recipe builds differs from them.
K = 10
K_MIN = 5
R_SCALE = 511
T_PARTNER = 0.01
MAC_MIN = 20
INFO_MIN = "none"
#: The threads each kind was built at, as its provenance's `build.threads` records.
THREADS = {"ld_store": 16, "genotype_store": 8}
#: build92.sh's environment for the engine: one BLAS thread, and no user site
#: packages (a user-site numpy shadows the environment's). `TMPDIR` is added per Store.
ENVIRONMENT: Mapping[str, str] = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "PYTHONNOUSERSITE": "1",
}


@dataclass(frozen=True)
class Inputs:
    """Where a panel's converted inputs are: `<pgen_root>/<PANEL>/chr<N>.{pgen,psam,pvar}`,
    `<keep_root>/<PANEL>.keep` and `<pcs_root>/<PANEL>.eigenvec` (opengwasdb's
    `scripts/ld/test_panel/roots.py` names)."""

    pgen_root: Path
    keep_root: Path
    pcs_root: Path
    chroms: tuple[int, ...] = CHROMS
    #: Where the published LD Stores are, `<root>/hgdp1kgp-v2-<PANEL>-k10.store`: a Genotype
    #: Store's staged copies are checked against its panel's LD Store's `source.sha256`.
    ld_stores_root: Path | None = None


def host_inputs() -> Inputs:
    """The inputs on the IEU host, each overridable by opengwasdb's variable for it."""
    def root(variable: str, default: str) -> Path:
        return Path(os.environ.get(variable, default))

    return Inputs(
        pgen_root=root("OPENGWASDB_LD_PGEN_V2", "/data/opengwasdb/work/ogld/phased_v2_pgen"),
        keep_root=root("OPENGWASDB_LD_KEEP", "/data/opengwasdb/work/ogld/keep"),
        pcs_root=root("OPENGWASDB_LD_PCS", "/data/opengwasdb/work/ogld/pcs"),
        ld_stores_root=root("OPENGWASDB_LD_STORES", "/data/opengwasdb/work/ogld/stores"),
    )


def panel_id(panel: str) -> str:
    return f"hgdp1kgp-v2-{panel}"


def store_name(panel: str) -> str:
    return f"{panel_id(panel)}-k{K}.store"


@dataclass(frozen=True)
class Plan:
    """One build: the Store it writes, the inputs it stages, and its two engine commands."""

    kind: str
    store: Path
    copies: tuple[tuple[Path, Path], ...]
    build: tuple[str, ...]
    validate: tuple[str, ...]
    #: The LD Store whose `source.sha256` the staged copies must match; a Genotype Store's alone.
    checksums_from: Path | None = None

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        return (self.build, self.validate)

    @property
    def script(self) -> str:
        """The record's `rebuild.script`: the engine subcommand the build runs."""
        return "opengwasdb ld build --genotypes" if self.kind == "genotype_store" else "opengwasdb ld build"


def plan_build(kind: str, panel: str, out: Path, inputs: Inputs, threads: int | None = None) -> Plan:
    """The build of `panel`'s Store of `kind` into the directory `out`, as `out/<panel id>-k10.store`."""
    if kind not in record.KINDS:
        raise ValueError(f"{kind!r} is not one of {', '.join(record.KINDS)}")
    threads = THREADS[kind] if threads is None else threads
    out = Path(out)
    store = out / store_name(panel)
    pgens = [inputs.pgen_root / panel / f"chr{chrom}.pgen" for chrom in inputs.chroms]
    keep = inputs.keep_root / f"{panel}.keep"
    pcs = inputs.pcs_root / f"{panel}.eigenvec"
    copies: list[tuple[Path, Path]] = []
    if kind == "genotype_store":
        for pgen in pgens:
            copies += [(pgen.with_suffix(f".{ext}"), out / "pgen" / pgen.with_suffix(f".{ext}").name)
                       for ext in ("pgen", "psam", "pvar")]
        copies += [(keep, out / "inputs" / keep.name), (pcs, out / "inputs" / pcs.name)]
        staged = {source: target for source, target in copies}
        pgens, keep, pcs = [staged[p] for p in pgens], staged[keep], staged[pcs]

    build = ["opengwasdb", "ld", "build", str(store)]
    if kind == "genotype_store":
        build.append("--genotypes")
    for pgen in pgens:
        build += ["--pgen", str(pgen)]
    build += [
        "--keep", str(keep),
        "--pcs", str(pcs),
        # The PC Files were made by ogld's make_pcs.py, and every Store's provenance says so.
        "--pc-source", f"ogld scripts/make_pcs.py on HGDP+1kGP v2 {panel} (ADR 0002)",
        "--panel-id", panel_id(panel),
        "--ancestry", panel,
        "--genotype-source", f"HGDP+1kGP phased v2 (Koenig et al. 2024), {panel}, converted by build-inputs §3",
        "--k", str(K),
        "--k-min", str(K_MIN),
        "--r-scale", str(R_SCALE),
        "--t-partner", str(T_PARTNER),
        "--mac-min", str(MAC_MIN),
        "--info-min", INFO_MIN,
        "--threads", str(threads),
    ]  # fmt: skip
    validate = ("opengwasdb", "ld", "validate", "--threads", str(threads), str(store))
    checksums_from = None
    if kind == "genotype_store" and inputs.ld_stores_root is not None:
        checksums_from = inputs.ld_stores_root / store_name(panel)
    return Plan(kind, store, tuple(copies), tuple(build), validate, checksums_from)


def rebuild_section(
    commands: Sequence[Sequence[str]],
    version: str,
    environment: str,
    script: str,
    inputs: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """The record's `rebuild` a recipe writes: this repository at `version`, `script`, the engine `commands`
    as run from its root, and the files they read (`inputs`, by name, each its `path`) when given.

    The writer pins each input's sha256 from the Store's own `source.sha256` (#542).
    """
    section: dict[str, Any] = {
        "repository": REPOSITORY,
        "version": version,
        "script": script,
        "commands": [shlex.join(["pixi", "run", "-e", environment, *command]) for command in commands],
    }
    if inputs is not None:
        section["inputs"] = {name: dict(entry) for name, entry in inputs.items()}
    return section


class RecipeRefusal(ValueError):
    """The recipe will not run; the message names what it refuses."""


#: Runs one engine command in an environment added to this one, raising when it fails; tests pass a stub.
CommandRunner = Callable[[Sequence[str], Mapping[str, str]], None]
#: Copies one input, keeping its modification time, and checks it against a sha256 when given one.
Copier = Callable[..., None]


def run_command(argv: Sequence[str], environment: Mapping[str, str] | None = None) -> None:
    """Run `argv` with `environment` added, its output to this terminal, and say how long it took."""
    print(f"$ {shlex.join(argv)}", flush=True)
    started = time.monotonic()
    done = subprocess.run(list(argv), check=False, env={**os.environ, **(environment or {})})
    print(f"  exit {done.returncode} after {time.monotonic() - started:.0f}s", flush=True)
    if done.returncode != 0:
        raise RecipeRefusal(f"`{shlex.join(argv)}` exited {done.returncode}")


def run_in_environment(commands: Sequence[Sequence[str]], store: Path, run: CommandRunner) -> None:
    """Run `commands` in build92.sh's environment, `TMPDIR` beside `store`, removed once all succeed."""
    tmpdir = store.parent / f".{store.name}.tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for command in commands:
        run(command, {**ENVIRONMENT, "TMPDIR": str(tmpdir)})
    shutil.rmtree(tmpdir)


#: The engine change that adds `opengwasdb ld import --plan-only` (#542).
PLAN_ONLY_CHANGE = "opengwas/opengwasdb#502 (opengwasdb-stores#542)"
#: The probe: an engine without the option refuses it by its CLI's own error before printing any help.
PLAN_ONLY_PROBE = ("opengwasdb", "ld", "import", "--plan-only", "--help")


def require_plan_only(runner: Runner | None = None) -> None:
    """Refuse an engine without `opengwasdb ld import --plan-only`, from the CLI's own answer.

    The probe `PLAN_ONLY_PROBE` exits 0 when the option exists. An engine without
    it exits non-zero with its "No such option" error naming `--plan-only`, which
    is refused naming `PLAN_ONLY_CHANGE`; any other failure, an engine not on
    PATH included, is refused with its own error. Nothing is tried instead.
    """
    try:
        done = run_engine(PLAN_ONLY_PROBE, runner=runner)
    except OSError as exc:
        raise RecipeRefusal(f"`{shlex.join(PLAN_ONLY_PROBE)}` could not run: {exc}") from exc
    if done.returncode == 0:
        return
    said = f"{done.stderr}\n{done.stdout}"
    if re.search(r"No such option\W*--plan-only\b", said, re.IGNORECASE):
        raise RecipeRefusal(
            f"the engine on PATH has no `opengwasdb ld import --plan-only`, which {PLAN_ONLY_CHANGE} adds; "
            "run against an engine with it, or wait for the lock to move to one"
        )
    raise RecipeRefusal(f"`{shlex.join(PLAN_ONLY_PROBE)}` exited {done.returncode}: {said.strip()}")


class Recording(NamedTuple):
    """What a record's `rebuild` names besides the commands: the commit, and the pixi environment."""

    version: str
    environment: str


def refuse_before_running(store: Path, inputs: Iterable[Path], *, staged: Iterable[Path] = ()) -> None:
    """Every recipe's prelude: a Store that exists, a staged input that exists, and a missing input are refused."""
    if store.exists():
        raise RecipeRefusal(f"{store} exists; a Store is never replaced")
    present = [str(target) for target in staged if target.exists()]
    if present:
        raise RecipeRefusal(f"{', '.join(present)} exist; a build stages its own inputs")
    missing = [str(path) for path in inputs if not path.exists()]
    if missing:
        raise RecipeRefusal(f"missing inputs: {', '.join(missing)}")


def refuse_before_recording(
    store: Path,
    inputs: Iterable[Path],
    *,
    ld_root: Path,
    ld_id: str,
    staged: Iterable[Path] = (),
    version: str | None = None,
    environment: str | None = None,
) -> Recording:
    """The prelude of a recipe that writes `ld_id`'s record, and what that record's `rebuild` names.

    Beyond `refuse_before_running`, it refuses an id that is not `OGL-` and five
    digits and one whose record already holds a Store (the writer would refuse
    either after the run), and a dirty checkout. It returns the version
    (`repository_version`) and environment (`PIXI_ENVIRONMENT_NAME`, default
    `dev`) the record names, either given.
    """
    try:
        require_valid_ld_id(ld_id)
    except ValueError as exc:
        raise RecipeRefusal(str(exc)) from exc
    refuse_before_running(store, inputs, staged=staged)
    path = record.record_path(Path(ld_root), ld_id)
    if path.exists():
        document, _ = record.read_document(path)
        if document and document.get("location"):
            raise RecipeRefusal(f"{path}: {ld_id} already records the Store at {document['location']}")
    return Recording(
        version or repository_version(),
        environment or os.environ.get("PIXI_ENVIRONMENT_NAME", "dev"),
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 24), b""):
            digest.update(block)
    return digest.hexdigest()


def stage_copy(source: Path, target: Path, sha256: str | None = None) -> None:
    """A read-only copy of `source` with its modification time, as build92.sh's `cp --preserve=timestamps`.

    Given `sha256`, the copy must have it, as build92.sh checked its copies
    against the published LD Store's source checksums; one that does not is
    removed and refused.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if sha256 is not None and (found := sha256_of(target)) != sha256:
        target.unlink()
        raise RecipeRefusal(f"{target}: sha256 {found}, but the LD Store read {target.name} as {sha256}")
    target.chmod(target.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def repository_version(repository: Path = REPO_ROOT) -> str:
    """The commit this checkout is at, as `git describe` names it (every merge to `main` is tagged).

    A checkout with uncommitted changes or untracked files is refused: a
    recorded `rebuild.version` must name a commit the Store can be rebuilt from,
    and an untracked module would run without being in it.
    """
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repository, capture_output=True, text=True, check=True).stdout

    changed = [line[3:] for line in git("status", "--porcelain", "--untracked-files=all").splitlines() if line]
    if changed:
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        raise RecipeRefusal(
            f"{repository} has uncommitted changes or untracked files ({shown}); a build records the commit it "
            "ran at, so commit or remove them"
        )
    return git("describe", "--tags", "--always").strip()


def staged_checksums(plan: Plan, info_runner: Runner | None) -> dict[Path, str]:
    """Each staged copy's sha256, from the published LD Store's provenance through `opengwasdb ld info`.

    As build92.sh did, the pgen family and the keep file are checked against
    `source.sha256`. The PC File is not in it: an LD Store records its digest
    as `adjustment.pc_file.sha256`. A provenance without either is refused,
    naming the field.
    """
    if not plan.copies:
        return {}
    if plan.checksums_from is None:
        raise RecipeRefusal(
            "a Genotype Store's staged inputs are checked against its LD Store's source.sha256; name the LD Stores' root"
        )
    provenance = ld_info(plan.checksums_from, runner=info_runner)

    def field(*keys: str) -> Any:
        value: Any = provenance
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        return value

    recorded, pc_file = field("source", "sha256"), field("adjustment", "pc_file", "sha256")
    if not isinstance(recorded, dict):
        raise RecipeRefusal(f"{plan.checksums_from} records no source.sha256: the staged copies cannot be checked")
    pcs = Path(argv_values(plan.build, "--pcs")[0])
    checksums: dict[Path, str] = {}
    for _, target in plan.copies:
        if target == pcs:
            if not isinstance(pc_file, str):
                raise RecipeRefusal(f"{plan.checksums_from} records no adjustment.pc_file.sha256 to check {target.name}")
            checksums[target] = pc_file
        elif target.name in recorded:
            checksums[target] = recorded[target.name]
    unread = [target.name for _, target in plan.copies if target not in checksums]
    if unread:
        raise RecipeRefusal(f"{plan.checksums_from} read no {', '.join(unread)}: its source.sha256 cannot check them")
    return checksums


def _made_by_staging(plan: Plan) -> list[Path]:
    """The directories staging will make: each target's ancestors that do not exist yet, deepest first."""
    made: set[Path] = set()
    for _, target in plan.copies:
        for directory in target.parents:
            if directory.exists():
                break
            made.add(directory)
    return sorted(made, key=lambda path: len(path.parts), reverse=True)


def build_store(
    plan: Plan,
    ld_root: Path,
    ld_id: str,
    *,
    run: CommandRunner = run_command,
    info_runner: Runner | None = None,
    version: str | None = None,
    environment: str | None = None,
    size: Callable[[Path], int] = store_size_on_disk,
    copy: Copier = stage_copy,
) -> Written:
    """Stage, build, validate, and write `ld_id`'s record with the build's `rebuild`.

    It refuses, before running anything, what `refuse_before_recording` does. A
    staged copy whose sha256 is not the LD Store's refuses the build, and the
    copies staged so far are removed.
    """
    read = [Path(value) for flag, value in argv_options(plan.build) if flag in ("--pgen", "--keep", "--pcs")]
    recording = refuse_before_recording(
        plan.store, [source for source, _ in plan.copies] if plan.copies else read,
        staged=[target for _, target in plan.copies], ld_root=Path(ld_root), ld_id=ld_id,
        version=version, environment=environment,
    )
    checksums = staged_checksums(plan, info_runner)
    # Every target was refused above if it existed, so whatever is at one now, a partial copy
    # included, staging made; so are the directories `_made_by_staging` names. Nothing else is removed.
    made = _made_by_staging(plan)
    try:
        for source, target in plan.copies:
            copy(source, target, checksums[target])
    except (RecipeRefusal, OSError):
        for _, target in plan.copies:
            if target.exists() or target.is_symlink():
                target.unlink()
        for directory in made:
            if directory.is_dir():
                directory.rmdir()
        raise
    run_in_environment(plan.commands, plan.store, run)
    return write_record(
        ld_root, ld_id, plan.store, runner=info_runner, size=size,
        rebuild=rebuild_section(plan.commands, *recording, plan.script),
    )
