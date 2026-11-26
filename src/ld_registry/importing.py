"""The Pan-UKB LD Store import recipe (#542; docs/spec/ld-store-registry.md, "The import recipe").

From an ancestry's four inputs (the BlockMatrix metadata, the exported variant
table, the liftover chain and the GRCh38 FASTA), it imports a Pan-UKB LD Store
through the engine's command line alone (ADR 0023), as ogld#10 did by hand:

1. `opengwasdb ld import`, which plans, downloads, closes each chromosome and
   joins them (opengwasdb build-inputs §12);
2. `opengwasdb ld validate`, once;
3. the record writer, recording these commands and the four inputs as
   `rebuild`; the writer pins each input's digest from the Store's own
   `source.sha256`.

With `--plan-only` it runs only the engine's `import --plan-only`, which writes
the plan and the lifted tables with no network access, and writes no record:
the plan is compared with another (`compare.plan_differences`). An engine
without the option is refused from its CLI's own error
(`recipe.require_plan_only`), never worked around.

The engine's importer reads Pan-UKB EUR alone (its URL, panel and n are fixed),
so every other ancestry is refused until opengwas/ogld#104. The variant export,
which needs Hail, stays in opengwasdb (`scripts/ld/export_panukb_variants.py`),
input preparation as the plink2 conversion is (#541).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ld_registry import recipe
from ld_registry.recipe import CommandRunner, RecipeRefusal, run_command, run_in_environment
from ld_registry.writer import Written, write_record
from ogstores.store_info import Runner
from ogstores.store_size import store_size_on_disk

#: The ancestries the engine's importer can read: its source URL and panel are Pan-UKB EUR's.
ANCESTRIES: tuple[str, ...] = ("EUR",)
ANCESTRY_ISSUE = "opengwas/ogld#104"
#: The four inputs, as the engine's options and its plan identity's `inputs` name them, in its order.
INPUTS: tuple[str, ...] = ("metadata", "variants", "chain", "fasta")
SCRIPT = "opengwasdb ld import"


def require_ancestry(ancestry: str) -> None:
    if ancestry not in ANCESTRIES:
        raise RecipeRefusal(
            f"ancestry {ancestry!r}: the engine's importer reads Pan-UKB {', '.join(ANCESTRIES)} alone; "
            f"other ancestries wait for {ANCESTRY_ISSUE}"
        )


def plan_path(work: Path) -> Path:
    """The plan an import writes in its work directory (opengwasdb build-inputs §12)."""
    return Path(work) / "plan.json"


@dataclass(frozen=True)
class ImportPlan:
    """One import: the Store it writes, its work directory, its inputs, and its engine commands."""

    store: Path
    work: Path
    inputs: Mapping[str, Path]
    command: tuple[str, ...]
    validate: tuple[str, ...]
    plan_only: bool = False

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        """What runs: the import and its validation, or a plan-only import alone."""
        return (self.command,) if self.plan_only else (self.command, self.validate)

    @property
    def plan_path(self) -> Path:
        return plan_path(self.work)


def plan_import(
    ancestry: str,
    *,
    out: Path,
    work: Path,
    metadata: Path,
    variants: Path,
    chain: Path,
    fasta: Path,
    parts: Path | None = None,
    delete_parts: bool = False,
    download_workers: int | None = None,
    threads: int | None = None,
    plan_only: bool = False,
) -> ImportPlan:
    """The import of `ancestry`'s Store at `out`, in #10's argument order. An option not given is the engine's default."""
    require_ancestry(ancestry)
    inputs = {"metadata": Path(metadata), "variants": Path(variants), "chain": Path(chain), "fasta": Path(fasta)}
    command = ["opengwasdb", "ld", "import", str(out), "--work", str(work)]
    for name in INPUTS:
        command += [f"--{name}", str(inputs[name])]
    if parts is not None:
        command += ["--parts", str(parts)]
    if delete_parts:
        command.append("--delete-parts")
    if download_workers is not None:
        command += ["--download-workers", str(download_workers)]
    if threads is not None:
        command += ["--threads", str(threads)]
    if plan_only:
        command.append("--plan-only")
    validate = recipe.validate_command(out, threads)
    return ImportPlan(Path(out), Path(work), inputs, tuple(command), validate, plan_only)


def rebuild_section(plan: ImportPlan, version: str, environment: str) -> dict[str, Any]:
    """The import's `rebuild` (`recipe.rebuild_section`), naming each input by its path."""
    inputs = {name: {"path": str(plan.inputs[name])} for name in INPUTS}
    return recipe.rebuild_section(plan.commands, version, environment, SCRIPT, inputs=inputs)


def plan_store(plan: ImportPlan, *, run: CommandRunner = run_command, help_runner: Runner | None = None) -> Path:
    """Run `plan`'s plan-only import, and return its plan's path. It writes no record and creates no Store."""
    if not plan.plan_only:
        raise ValueError("plan_store runs a plan-only import; import_store runs a full one")
    recipe.refuse_before_running(plan.store, plan.inputs.values())
    recipe.require_plan_only(help_runner)
    run_in_environment(plan.commands, plan.store, run)
    return plan.plan_path


def import_store(
    plan: ImportPlan,
    ld_root: Path,
    ld_id: str,
    *,
    run: CommandRunner = run_command,
    info_runner: Runner | None = None,
    version: str | None = None,
    environment: str | None = None,
    size: Callable[[Path], int] = store_size_on_disk,
) -> Written:
    """Import, validate once, and write `ld_id`'s record with the import's `rebuild`.

    It refuses a plan-only plan, then, before running anything, what
    `recipe.refuse_before_recording` does. The writer pins each input's digest
    from the Store's `source.sha256`, and refuses a Store that holds none of one.
    """
    if plan.plan_only:
        raise RecipeRefusal("a plan-only import writes no record; run it with plan_store")
    recording = recipe.refuse_before_recording(
        plan.store, plan.inputs.values(), ld_root=Path(ld_root), ld_id=ld_id, version=version, environment=environment
    )
    run_in_environment(plan.commands, plan.store, run)
    return write_record(
        ld_root, ld_id, plan.store, runner=info_runner, size=size, rebuild=rebuild_section(plan, *recording),
    )
