#!/usr/bin/env python3
"""The HGDP+1kGP LD Store and Genotype Store recipe (#541, epic #496; docs/spec/ld-store-registry.md).

Three seams, each with a stub runner, so no test here runs the engine:

1. the recipe's command lines: for each kind, the argv it would run equals a
   literal list. The Genotype Store's is OGL-00009's recorded `ogld build`
   (build92.sh's flags), modernised to `opengwasdb ld`. No LD Store record
   carries a flag list (theirs ran `scripts/build_hgdp1kgp.py build`), so the
   LD Store's literal is build92.sh's flags without `--genotypes`, at the 16
   threads, inputs and PC source the LD Stores' own provenance records;
2. rebuilding from a record: its refusals, and that it runs the record's
   commands with only the output changed;
3. the acceptance comparison of two `opengwasdb ld info` outputs.

`test_tiny_build.py` runs one real build. Run from the repository root:
    python3 tests/ld-registry/test_ld_stores.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from ld_registry import compare, rebuild, recipe, record, writer
from ld_registry.provenance import provenance_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GENOTYPE_INFO = FIXTURES / "ld-info-amr-genotype-store.json"

AMR_DIR = "/data/opengwasdb/ld-stores/hgdp1kgp-v2-AMR"
AMR_GENOTYPE_STORE = f"{AMR_DIR}/hgdp1kgp-v2-AMR-k10.store"

#: OGL-00009's `rebuild.commands[0]`, as committed (#539), from build92.sh.
OGL_00009_COMMAND = (
    "pixi run -e dev ogld build /data/opengwasdb/ld-stores/hgdp1kgp-v2-AMR/hgdp1kgp-v2-AMR-k10.store --genotypes"
    + "".join(f" --pgen /data/opengwasdb/ld-stores/hgdp1kgp-v2-AMR/pgen/chr{c}.pgen" for c in range(1, 23))
    + " --keep /data/opengwasdb/ld-stores/hgdp1kgp-v2-AMR/inputs/AMR.keep"
    " --pcs /data/opengwasdb/ld-stores/hgdp1kgp-v2-AMR/inputs/AMR.eigenvec"
    " --pc-source 'ogld scripts/make_pcs.py on HGDP+1kGP v2 AMR (ADR 0002)' --panel-id hgdp1kgp-v2-AMR"
    " --ancestry AMR --genotype-source 'HGDP+1kGP phased v2 (Koenig et al. 2024), AMR, converted by build-inputs §3'"
    " --k 10 --k-min 5 --r-scale 511 --t-partner 0.01 --mac-min 20 --info-min none --threads 8"
)

#: The AMR LD Store (OGL-00003), at its location, read straight from the converted inputs.
AMR_LD_COMMAND = (
    "opengwasdb ld build /data/opengwasdb/work/ogld/stores/hgdp1kgp-v2-AMR-k10.store"
    + "".join(f" --pgen /data/opengwasdb/work/ogld/phased_v2_pgen/AMR/chr{c}.pgen" for c in range(1, 23))
    + " --keep /data/opengwasdb/work/ogld/keep/AMR.keep --pcs /data/opengwasdb/work/ogld/pcs/AMR.eigenvec"
    " --pc-source 'ogld scripts/make_pcs.py on HGDP+1kGP v2 AMR (ADR 0002)' --panel-id hgdp1kgp-v2-AMR"
    " --ancestry AMR --genotype-source 'HGDP+1kGP phased v2 (Koenig et al. 2024), AMR, converted by build-inputs §3'"
    " --k 10 --k-min 5 --r-scale 511 --t-partner 0.01 --mac-min 20 --info-min none --threads 16"
)

HOST = recipe.Inputs(
    pgen_root=Path("/data/opengwasdb/work/ogld/phased_v2_pgen"),
    keep_root=Path("/data/opengwasdb/work/ogld/keep"),
    pcs_root=Path("/data/opengwasdb/work/ogld/pcs"),
    ld_stores_root=Path("/data/opengwasdb/work/ogld/stores"),
)
#: The environment build92.sh exported, which the recipe sets for every engine command.
ENVIRONMENT = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "PYTHONNOUSERSITE": "1"}


def recorded_command(ld_id: str, number: int = 0) -> str:
    """A committed record's `rebuild.commands[number]`, as #539 recorded it."""
    path = REPO_ROOT / record.LD_ROOT / ld_id / record.RECORD_FILENAME
    return yaml.safe_load(path.read_text(encoding="utf-8"))["rebuild"]["commands"][number]


def genotype_info() -> dict[str, Any]:
    """OGL-00009's `ld info`; the fixture is trimmed of its PC Check, whose `dropped` is `[]` in the Store."""
    info = json.loads(GENOTYPE_INFO.read_text(encoding="utf-8"))
    info["adjustment"]["pc_check"] = {"dropped": []}
    return info


def ogl_00009(rebuild_section: dict[str, Any] | None = None) -> dict[str, Any]:
    """OGL-00009 as committed, trimmed to what a rebuild reads."""
    return {
        "ld_id": "OGL-00009",
        "kind": "genotype_store",
        "rebuild": rebuild_section if rebuild_section is not None else {
            "repository": "opengwas/ogld",
            "version": "0.5.0",
            "script": "ogld build --genotypes",
            "commands": [OGL_00009_COMMAND],
        },
        "location": AMR_GENOTYPE_STORE,
    }


class Commands:
    """A stub runner: records each argv and its environment, and a build makes only its Store's empty directory."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, argv, environment=None) -> None:
        self.seen.append(list(argv))
        if environment is not None:
            self.environments.append(dict(environment))
            assert Path(environment["TMPDIR"]).is_dir(), "TMPDIR exists while a command runs"
        if list(argv[:3]) == ["opengwasdb", "ld", "build"]:
            Path(argv[3]).mkdir(parents=True)


class Copies:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def __call__(self, source: Path, target: Path, sha256: str | None = None) -> None:
        self.seen.append((str(source), str(target)))


class TestTheRecipesCommandLines(unittest.TestCase):
    """Seam 1."""

    def test_the_literal_is_ogl_00009s_record(self) -> None:
        self.assertEqual(OGL_00009_COMMAND, recorded_command("OGL-00009"))

    def test_a_genotype_store_is_ogl_00009s_command_modernised(self) -> None:
        plan = recipe.plan_build("genotype_store", "AMR", Path(AMR_DIR), HOST)
        recorded = shlex.split(recorded_command("OGL-00009"))
        self.assertEqual(recorded[:4], ["pixi", "run", "-e", "dev"])
        self.assertEqual(recorded[4], "ogld")
        self.assertEqual(list(plan.build), ["opengwasdb", "ld", *recorded[5:]])

    def test_a_genotype_store_stages_its_inputs_beside_the_sidecar(self) -> None:
        """build92.sh: each chromosome's pgen, psam and pvar into pgen/, the keep and PC files into inputs/."""
        plan = recipe.plan_build("genotype_store", "AMR", Path(AMR_DIR), HOST)
        expected = [
            (f"/data/opengwasdb/work/ogld/phased_v2_pgen/AMR/chr{c}.{e}", f"{AMR_DIR}/pgen/chr{c}.{e}")
            for c in range(1, 23) for e in ("pgen", "psam", "pvar")
        ] + [
            ("/data/opengwasdb/work/ogld/keep/AMR.keep", f"{AMR_DIR}/inputs/AMR.keep"),
            ("/data/opengwasdb/work/ogld/pcs/AMR.eigenvec", f"{AMR_DIR}/inputs/AMR.eigenvec"),
        ]
        self.assertEqual([(str(s), str(t)) for s, t in plan.copies], expected)
        self.assertEqual(str(plan.store), AMR_GENOTYPE_STORE)
        self.assertEqual(plan.checksums_from, Path("/data/opengwasdb/work/ogld/stores/hgdp1kgp-v2-AMR-k10.store"),
                         "build92.sh checks the copies against the published LD Store's source checksums")

    def test_an_ld_store_reads_the_inputs_where_they_are(self) -> None:
        plan = recipe.plan_build("ld_store", "AMR", Path("/data/opengwasdb/work/ogld/stores"), HOST)
        self.assertEqual(list(plan.build), shlex.split(AMR_LD_COMMAND))
        self.assertEqual(plan.copies, ())

    def test_validation_follows_at_the_builds_threads(self) -> None:
        for kind, threads in (("ld_store", "16"), ("genotype_store", "8")):
            with self.subTest(kind=kind):
                plan = recipe.plan_build(kind, "AMR", Path("/o"), HOST)
                self.assertEqual(
                    list(plan.validate), ["opengwasdb", "ld", "validate", "--threads", threads, "/o/hgdp1kgp-v2-AMR-k10.store"]
                )

    def test_threads_can_be_set(self) -> None:
        plan = recipe.plan_build("ld_store", "EUR", Path("/o"), HOST, threads=4)
        self.assertEqual(plan.build[-2:], ("--threads", "4"))
        self.assertEqual(plan.validate[3:5], ("--threads", "4"))

    def test_the_rebuild_it_records_is_its_own_commands(self) -> None:
        plan = recipe.plan_build("genotype_store", "AMR", Path(AMR_DIR), HOST)
        section = recipe.rebuild_section(plan.commands, version="v0.47.0", environment="dev", script=plan.script)
        self.assertEqual(section["repository"], "opengwas/opengwasdb-stores")
        self.assertEqual(section["version"], "v0.47.0")
        self.assertEqual(section["script"], "opengwasdb ld build --genotypes")
        self.assertEqual(
            section["commands"],
            [recorded_command("OGL-00009").replace("ogld build", "opengwasdb ld build"),
             f"pixi run -e dev opengwasdb ld validate --threads 8 {AMR_GENOTYPE_STORE}"],
        )
        self.assertNotIn("inputs", section, "a build names no inputs")
        self.assertEqual(recipe.plan_build("ld_store", "AMR", Path("/o"), HOST).script, "opengwasdb ld build")


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.ld_root = self.tmp / "stores" / "ld"

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestTheRecipeBuilds(TempCase):
    """The recipe stages, builds, validates, then writes the record with its `rebuild`."""

    def inputs(self) -> recipe.Inputs:
        root = self.tmp / "in"
        for chrom in (21, 22):
            for ext in ("pgen", "psam", "pvar"):
                path = root / "pgen" / "T" / f"chr{chrom}.{ext}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{chrom}.{ext}\n")
        (root / "keep").mkdir(exist_ok=True)
        (root / "keep" / "T.keep").write_text("#IID\n")
        (root / "pcs").mkdir(exist_ok=True)
        (root / "pcs" / "T.eigenvec").write_text("#IID\tPC1\n")
        return recipe.Inputs(root / "pgen", root / "keep", root / "pcs", chroms=(21, 22), ld_stores_root=root / "stores")

    def checksums(self, inputs: recipe.Inputs) -> dict[str, Any]:
        """The published LD Store's provenance, in OGL-00003's shape (`ld-info-amr-ld-store-checksums.json`):
        `source.sha256` holds the pgen family and the keep file, and the PC File's is `adjustment.pc_file.sha256`."""
        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        files = [inputs.pgen_root / "T" / f"chr{c}.{e}" for c in (21, 22) for e in ("pgen", "psam", "pvar")]
        files += [inputs.keep_root / "T.keep"]
        return {
            "source": {"kind": "genotypes", "sha256": {path.name: digest(path) for path in files}},
            "adjustment": {"pc_file": {"sha256": digest(inputs.pcs_root / "T.eigenvec"), "source": "tests"}},
        }

    def build(self, plan: recipe.Plan, commands: Commands, info: dict[str, Any] | None = None,
              checksums: dict[str, Any] | None = None, copy: Any = recipe.stage_copy):
        def info_runner(argv):
            commands.seen.append(list(argv))
            if plan.checksums_from is not None and argv[-1] == str(plan.checksums_from):
                payload = checksums or {}
            else:
                payload = info or json.loads((FIXTURES / "ld-info-eur.json").read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        return recipe.build_store(
            plan, self.ld_root, "OGL-00099", run=commands, info_runner=info_runner, version="v0.47.0",
            environment="dev", size=lambda _: 1, copy=copy,
        )

    def pinned(self) -> dict[str, Any]:
        info = genotype_info()  # pinning the staged copies, as the sidecar would
        info["filesets"] = [
            {ext: {"path": f"../pgen/chr{chrom}.{ext}", "size": len(f"{chrom}.{ext}\n")} for ext in ("pgen", "psam", "pvar")}
            for chrom in (21, 22)
        ]
        return info

    def test_it_builds_validates_and_writes_the_record(self) -> None:
        plan = recipe.plan_build("ld_store", "T", self.tmp / "out", self.inputs())
        commands = Commands()
        self.build(plan, commands)
        self.assertEqual(
            commands.seen,
            [list(plan.build), list(plan.validate), ["opengwasdb", "ld", "info", str(plan.store)]],
        )
        written = yaml.safe_load(record.record_path(self.ld_root, "OGL-00099").read_text(encoding="utf-8"))
        self.assertEqual(written["rebuild"], recipe.rebuild_section(plan.commands, "v0.47.0", "dev", plan.script))
        self.assertEqual(written["location"], str(plan.store))

    def test_it_sets_build92s_environment_with_a_tmpdir_beside_the_store(self) -> None:
        plan = recipe.plan_build("ld_store", "T", self.tmp / "out", self.inputs())
        commands = Commands()
        self.build(plan, commands)
        tmpdir = str(self.tmp / "out" / ".hgdp1kgp-v2-T-k10.store.tmp")
        self.assertEqual(commands.environments, [{**ENVIRONMENT, "TMPDIR": tmpdir}] * 2)
        self.assertFalse(Path(tmpdir).exists(), "a build that succeeded leaves no TMPDIR")

    def test_a_genotype_store_checks_each_staged_copy_against_the_ld_stores_checksums(self) -> None:
        inputs = self.inputs()
        plan = recipe.plan_build("genotype_store", "T", self.tmp / "out", inputs)
        commands = Commands()
        self.build(plan, commands, info=self.pinned(), checksums=self.checksums(inputs))
        self.assertEqual(commands.seen[0], ["opengwasdb", "ld", "info", str(inputs.ld_stores_root / "hgdp1kgp-v2-T-k10.store")])
        self.assertEqual([argv[2] for argv in commands.seen[1:3]], ["build", "validate"])

    def test_a_staged_copy_whose_checksum_differs_refuses_the_build(self) -> None:
        def changed(change) -> dict[str, Any]:
            provenance = self.checksums(self.inputs())
            change(provenance)
            return provenance

        for name, checksums, needle in (
            ("a different pvar", changed(lambda p: p["source"]["sha256"].update({"chr22.pvar": "0" * 64})), "chr22.pvar"),
            ("a different PC File", changed(lambda p: p["adjustment"]["pc_file"].update(sha256="0" * 64)), "T.eigenvec"),
            ("a keep file the LD Store did not read", changed(lambda p: p["source"]["sha256"].pop("T.keep")), "T.keep"),
            ("no PC File", changed(lambda p: p["adjustment"].update(pc_file=None)), "adjustment.pc_file.sha256"),
            ("a null source", changed(lambda p: p.update(source=None)), "source.sha256"),
            ("a source without sha256", changed(lambda p: p["source"].pop("sha256")), "source.sha256"),
        ):
            with self.subTest(name):
                out = self.tmp / name.replace(" ", "-")
                plan = recipe.plan_build("genotype_store", "T", out, self.inputs())
                commands = Commands()
                with self.assertRaises(recipe.RecipeRefusal) as caught:
                    self.build(plan, commands, info=self.pinned(), checksums=checksums)
                self.assertIn(needle, str(caught.exception))
                self.assertEqual([argv[2] for argv in commands.seen], ["info"], "no engine build or validate runs")
                self.assertFalse(out.exists(), "nothing staged is left, nor the directories staging made")

    def test_a_refusal_while_staging_removes_only_what_staging_made(self) -> None:
        out = self.tmp / "kept-out"
        out.mkdir()
        (out / "a-file-of-mine").write_text("mine\n")
        plan = recipe.plan_build("genotype_store", "T", out, self.inputs())
        checksums = self.checksums(self.inputs())
        checksums["source"]["sha256"]["chr22.pgen"] = "0" * 64
        with self.assertRaises(recipe.RecipeRefusal):
            self.build(plan, Commands(), info=self.pinned(), checksums=checksums)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["a-file-of-mine"])

    def test_an_os_error_while_staging_removes_what_staging_made_a_partial_copy_included(self) -> None:
        out = self.tmp / "out"
        plan = recipe.plan_build("genotype_store", "T", out, self.inputs())
        copied: list[Path] = []

        def failing_copy(source: Path, target: Path, sha256: str | None = None) -> None:
            if len(copied) == 4:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"half")  # a disk full partway through
                raise OSError(28, "No space left on device")
            recipe.stage_copy(source, target, sha256)
            copied.append(target)

        commands = Commands()
        with self.assertRaises(OSError):
            self.build(plan, commands, info=self.pinned(), checksums=self.checksums(self.inputs()), copy=failing_copy)
        self.assertEqual(len(copied), 4)
        self.assertFalse(out.exists())
        self.assertEqual([argv[2] for argv in commands.seen], ["info"])

    def test_a_genotype_store_needs_an_ld_store_to_check_against(self) -> None:
        inputs = self.inputs()
        plan = recipe.plan_build("genotype_store", "T", self.tmp / "out",
                                 recipe.Inputs(inputs.pgen_root, inputs.keep_root, inputs.pcs_root, chroms=(21, 22)))
        commands = Commands()
        with self.assertRaises(recipe.RecipeRefusal) as caught:
            self.build(plan, commands)
        self.assertIn("source.sha256", str(caught.exception))
        self.assertEqual(commands.seen, [])

    def test_a_dirty_checkout_is_refused_before_anything_runs(self) -> None:
        plan = recipe.plan_build("ld_store", "T", self.tmp / "out", self.inputs())
        commands = Commands()
        original = recipe.repository_version

        def dirty() -> str:
            raise recipe.RecipeRefusal("the checkout has uncommitted changes")

        recipe.repository_version = dirty
        try:
            with self.assertRaises(recipe.RecipeRefusal):
                recipe.build_store(plan, self.ld_root, "OGL-00099", run=commands, size=lambda _: 1)
        finally:
            recipe.repository_version = original
        self.assertEqual(commands.seen, [])

    def test_a_genotype_store_stages_read_only_copies_keeping_their_times(self) -> None:
        inputs = self.inputs()
        plan = recipe.plan_build("genotype_store", "T", self.tmp / "out", inputs)
        written = self.build(plan, Commands(), info=self.pinned(), checksums=self.checksums(inputs))
        self.assertEqual(
            yaml.safe_load(written.path.read_text(encoding="utf-8"))["measured"]["genotype_bytes"],
            sum(len(f"{chrom}.{ext}\n") for chrom in (21, 22) for ext in ("pgen", "psam", "pvar")),
        )
        for source, target in plan.copies:
            with self.subTest(target=str(target)):
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)
                self.assertEqual(target.stat().st_mode & 0o222, 0, "staged copies are read-only")

    def test_it_refuses_an_output_that_exists(self) -> None:
        for kind in ("ld_store", "genotype_store"):
            with self.subTest(kind=kind):
                plan = recipe.plan_build(kind, "T", self.tmp / kind, self.inputs())
                plan.store.mkdir(parents=True)
                commands = Commands()
                with self.assertRaises(recipe.RecipeRefusal) as caught:
                    self.build(plan, commands)
                self.assertIn(str(plan.store), str(caught.exception))
                self.assertEqual(commands.seen, [])

    def test_it_refuses_a_staged_input_that_exists(self) -> None:
        plan = recipe.plan_build("genotype_store", "T", self.tmp / "out", self.inputs())
        staged = plan.copies[0][1]
        staged.parent.mkdir(parents=True)
        staged.write_text("an older copy\n")
        commands = Commands()
        with self.assertRaises(recipe.RecipeRefusal) as caught:
            self.build(plan, commands)
        self.assertIn(str(staged), str(caught.exception))
        self.assertEqual(commands.seen, [])

    def test_it_refuses_a_missing_input_before_running_anything(self) -> None:
        inputs = self.inputs()
        (inputs.keep_root / "T.keep").unlink()
        commands = Commands()
        with self.assertRaises(recipe.RecipeRefusal) as caught:
            self.build(recipe.plan_build("ld_store", "T", self.tmp / "out", inputs), commands)
        self.assertIn("T.keep", str(caught.exception))
        self.assertEqual(commands.seen, [])

    def test_it_refuses_an_id_whose_record_holds_another_store(self) -> None:
        path = record.record_path(self.ld_root, "OGL-00099")
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump({"ld_id": "OGL-00099", "location": "/elsewhere/x.store"}))
        commands = Commands()
        with self.assertRaises(recipe.RecipeRefusal) as caught:
            self.build(recipe.plan_build("ld_store", "T", self.tmp / "out", self.inputs()), commands)
        self.assertIn("OGL-00099", str(caught.exception))
        self.assertEqual(commands.seen, [])


class TestTheRecordedVersion(TempCase):
    """A recorded `rebuild.version` names a commit: a dirty checkout is refused."""

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.tmp), *args], capture_output=True, text=True, check=True).stdout

    def setUp(self) -> None:
        super().setUp()
        self.git("init", "-q")
        (self.tmp / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "a")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.org", "tag", "v1.0.0")

    def test_a_clean_checkout_records_its_tag(self) -> None:
        self.assertEqual(recipe.repository_version(self.tmp), "v1.0.0")

    def test_an_untracked_file_is_refused(self) -> None:
        (self.tmp / "new_module.py").write_text("x = 1\n")
        with self.assertRaises(recipe.RecipeRefusal) as caught:
            recipe.repository_version(self.tmp)
        self.assertIn("new_module.py", str(caught.exception))

    def test_a_dirty_checkout_is_refused(self) -> None:
        (self.tmp / "a.txt").write_text("changed\n")
        with self.assertRaises(recipe.RecipeRefusal) as caught:
            recipe.repository_version(self.tmp)
        self.assertIn("uncommitted", str(caught.exception))


class TestRebuildingFromARecord(TempCase):
    """Seam 2: the record's commands, with only the output changed. The record and its Store are never written."""

    def records(self, *documents: dict[str, Any]) -> None:
        for document in documents:
            path = record.record_path(self.ld_root, document["ld_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def rebuild(self, ld_id: str, out: Path, commands: Commands | None = None, copies: Copies | None = None):
        return rebuild.rebuild_store(self.ld_root, ld_id, out, run=commands or Commands(), copy=copies or Copies())

    def test_it_runs_the_records_commands_with_only_the_output_changed(self) -> None:
        self.records(ogl_00009())
        before = record.record_path(self.ld_root, "OGL-00009").read_bytes()
        out = self.tmp / "scratch" / "amr"
        commands, copies = Commands(), Copies()
        rebuilt = self.rebuild("OGL-00009", out, commands, copies)
        expected = OGL_00009_COMMAND.replace(AMR_DIR, str(out)).replace("pixi run -e dev ogld", "opengwasdb ld")
        store = f"{out}/hgdp1kgp-v2-AMR-k10.store"
        self.assertEqual(
            commands.seen,
            [shlex.split(expected), ["opengwasdb", "ld", "validate", "--threads", "8", store]],
            "a record without a validate is validated once, at the build's threads",
        )
        self.assertEqual(commands.environments, [{**ENVIRONMENT, "TMPDIR": f"{out}/.hgdp1kgp-v2-AMR-k10.store.tmp"}] * 2)
        self.assertEqual((rebuilt.original, rebuilt.recorded_sha256), (Path(AMR_GENOTYPE_STORE), None))
        self.assertEqual(str(rebuilt.store), f"{out}/hgdp1kgp-v2-AMR-k10.store")
        self.assertEqual(record.record_path(self.ld_root, "OGL-00009").read_bytes(), before)

    def test_it_copies_the_inputs_the_commands_read_from_the_stores_directory(self) -> None:
        """A Genotype Store pins its pgens relative to its sidecar, so they move with it, times kept."""
        self.records(ogl_00009())
        out = self.tmp / "amr"
        copies = Copies()
        self.rebuild("OGL-00009", out, copies=copies)
        expected = [
            (f"{AMR_DIR}/pgen/chr{c}.{e}", f"{out}/pgen/chr{c}.{e}") for c in range(1, 23) for e in ("pgen", "psam", "pvar")
        ] + [(f"{AMR_DIR}/inputs/AMR.keep", f"{out}/inputs/AMR.keep"),
             (f"{AMR_DIR}/inputs/AMR.eigenvec", f"{out}/inputs/AMR.eigenvec")]
        self.assertEqual(copies.seen, expected)

    def test_a_record_the_recipe_wrote_rebuilds_the_same_way(self) -> None:
        plan = recipe.plan_build("ld_store", "AMR", Path("/data/opengwasdb/work/ogld/stores"), HOST)
        section = recipe.rebuild_section(plan.commands, "v0.47.0", "dev", plan.script)
        self.records({"ld_id": "OGL-00003", "kind": "ld_store", "rebuild": section, "location": str(plan.store)})
        out = self.tmp / "amr-ld"
        commands, copies = Commands(), Copies()
        self.rebuild("OGL-00003", out, commands, copies)
        new = f"{out}/hgdp1kgp-v2-AMR-k10.store"
        self.assertEqual(
            commands.seen,
            [[new if arg == str(plan.store) else arg for arg in plan.build],
             [new if arg == str(plan.store) else arg for arg in plan.validate]],
        )
        self.assertEqual(copies.seen, [], "an LD Store reads its inputs where they are")

    def test_it_refuses_an_output_that_exists(self) -> None:
        self.records(ogl_00009())
        out = self.tmp / "exists"
        out.mkdir()
        commands = Commands()
        with self.assertRaises(rebuild.RebuildRefusal) as caught:
            self.rebuild("OGL-00009", out, commands)
        self.assertIn(str(out), str(caught.exception))
        self.assertEqual(commands.seen, [])

    def test_it_refuses_a_record_without_a_runnable_rebuild(self) -> None:
        ogl_00001 = {
            "ld_id": "OGL-00001", "kind": "ld_store", "location": "/data/opengwasdb/work/ogld/stores/hgdp1kgp-v2-EUR-k10.store",
            "rebuild": {
                "repository": "opengwas/ogld", "version": "0.0.1.dev292+g25af8039f", "script": "scripts/build_hgdp1kgp.py",
                "commands": ["pixi run -e dev python scripts/build_hgdp1kgp.py convert --panel EUR",
                             "pixi run -e dev python scripts/build_hgdp1kgp.py build --panel EUR"],
            },
        }
        elsewhere = ogl_00009({**ogl_00009()["rebuild"], "commands": [
            OGL_00009_COMMAND.replace(AMR_GENOTYPE_STORE, "/elsewhere/x.store")]})
        no_build = ogl_00009({**ogl_00009()["rebuild"], "commands": [f"opengwasdb ld validate {AMR_GENOTYPE_STORE}"]})
        for name, document, needle in (
            ("a script, not the engine", ogl_00001, "scripts/build_hgdp1kgp.py"),
            ("no rebuild", {**ogl_00009(), "rebuild": None}, "rebuild"),
            ("no commands", ogl_00009({"repository": "x", "version": "1", "script": "x", "commands": []}), "rebuild.commands"),
            ("a build of another Store", elsewhere, "/elsewhere/x.store"),
            ("no build", no_build, "builds"),
            ("a shell", ogl_00009({**ogl_00009()["rebuild"], "commands": [OGL_00009_COMMAND + " && rm -rf /"]}), "&&"),
        ):
            with self.subTest(name):
                self.ld_root = self.tmp / name.replace(" ", "-") / "ld"
                self.records(document)
                commands = Commands()
                with self.assertRaises(rebuild.RebuildRefusal) as caught:
                    self.rebuild(document["ld_id"], self.tmp / name.replace(" ", "-") / "out", commands)
                self.assertIn(document["ld_id"], str(caught.exception))
                self.assertIn(needle, str(caught.exception))
                self.assertEqual(commands.seen, [])

    def test_it_refuses_an_output_reached_through_a_link_into_a_recorded_store(self) -> None:
        store = self.tmp / "ld-stores" / "x" / "hgdp1kgp-v2-AMR-k10.store"
        store.mkdir(parents=True)
        self.records({**ogl_00009(), "location": str(store)})
        (self.tmp / "link").symlink_to(store)
        commands = Commands()
        with self.assertRaises(rebuild.RebuildRefusal) as caught:
            self.rebuild("OGL-00009", self.tmp / "link" / "rebuilt", commands)
        self.assertIn("inside", str(caught.exception))
        self.assertEqual(commands.seen, [])

    def test_it_refuses_an_output_inside_any_recorded_stores_location(self) -> None:
        ld_store = {"ld_id": "OGL-00001", "kind": "ld_store", "location": str(self.tmp / "ogld" / "stores" / "eur.store")}
        self.records(ogl_00009(), ld_store)
        for out in (
            Path(AMR_GENOTYPE_STORE) / "rebuilt",
            Path(AMR_DIR) / "rebuilt",  # a Genotype Store's pins resolve into its directory
            Path(ld_store["location"]) / "x" / "y",
        ):
            with self.subTest(out=str(out)):
                commands = Commands()
                with self.assertRaises(rebuild.RebuildRefusal) as caught:
                    self.rebuild("OGL-00009", out, commands)
                self.assertIn("inside", str(caught.exception))
                self.assertEqual(commands.seen, [])

    def test_a_validate_with_threads_is_rewritten_too(self) -> None:
        section = {**ogl_00009()["rebuild"], "commands": [
            OGL_00009_COMMAND, f"pixi run -e dev ogld validate {AMR_GENOTYPE_STORE}"]}
        self.records(ogl_00009(section))
        commands = Commands()
        out = self.tmp / "o"
        self.rebuild("OGL-00009", out, commands)
        self.assertEqual(commands.seen[1:], [["opengwasdb", "ld", "validate", f"{out}/hgdp1kgp-v2-AMR-k10.store"]],
                         "the recorded validate, and no second")

    def test_the_plan_names_its_validation(self) -> None:
        """Acceptance validates the rebuilt Store at the build's threads, whether or not the record did."""
        self.records(ogl_00009())
        rebuilt = self.rebuild("OGL-00009", self.tmp / "o")
        self.assertEqual(list(rebuilt.validate), ["opengwasdb", "ld", "validate", "--threads", "8", str(rebuilt.store)])


class TestTheAcceptanceComparison(unittest.TestCase):
    """Seam 3: `ld info` of the original and of the rebuilt Store, field by field."""

    def rebuilt(self) -> dict[str, Any]:
        """OGL-00009's provenance as a rebuild at the locked engine would differ: the build's own fields."""
        info = copy.deepcopy(genotype_info())
        info["build"]["created_at"] = "2026-11-26T09:14:03Z"
        info["build"]["wall_seconds"] = 540.25
        info["build"]["peak_memory_bytes"] = 5200000000
        info["software"]["ogld"] = "0.20.0"
        info["software"]["zarr"] = "3.4.1"
        info["software"]["pgenlib"] = "0.95.2"
        return info

    def test_differences_only_in_the_builds_own_fields_pass(self) -> None:
        found = compare.differences(genotype_info(), self.rebuilt())
        self.assertEqual(
            sorted(d.field for d in found),
            ["build.created_at", "build.peak_memory_bytes", "build.wall_seconds",
             "software.ogld", "software.pgenlib", "software.zarr"],
        )
        self.assertTrue(all(d.build_own for d in found))
        self.assertEqual(compare.failures(found), [])

    def test_identical_provenance_has_no_difference(self) -> None:
        self.assertEqual(compare.differences(genotype_info(), genotype_info()), [])

    def test_a_different_n_fails_naming_the_field(self) -> None:
        rebuilt = self.rebuilt()
        rebuilt["panel"]["n"] = 340
        failed = compare.failures(compare.differences(genotype_info(), rebuilt))
        self.assertEqual([(d.field, d.original, d.rebuilt) for d in failed], [("panel.n", 341, 340)])
        self.assertIn("panel.n: 341 -> 340", compare.render(compare.differences(genotype_info(), rebuilt)))

    def test_a_field_one_side_lacks_fails(self) -> None:
        rebuilt = self.rebuilt()
        del rebuilt["t_partner"]
        rebuilt["filesets"][1]["pgen"]["path"] = "../elsewhere/chr2.pgen"
        rebuilt["arrays"]["extra"] = {"shape": [1]}
        failed = compare.failures(compare.differences(genotype_info(), rebuilt))
        self.assertEqual(
            [(d.field, d.original, d.rebuilt) for d in failed],
            [("arrays.extra", compare.ABSENT, {"shape": [1]}),
             ("filesets[1].pgen.path", "../pgen/chr2.pgen", "../elsewhere/chr2.pgen"),
             ("t_partner", 0.01, compare.ABSENT)],
        )

    def test_a_build_field_one_side_lacks_fails(self) -> None:
        for field, side in (("created_at", "rebuilt"), ("peak_memory_bytes", "original")):
            with self.subTest(field=field):
                original, rebuilt = genotype_info(), self.rebuilt()
                del (rebuilt if side == "rebuilt" else original)["build"][field]
                self.assertEqual([d.field for d in compare.failures(compare.differences(original, rebuilt))],
                                 [f"build.{field}"])

    def test_a_software_version_one_side_lacks_fails(self) -> None:
        rebuilt = self.rebuilt()
        rebuilt["software"]["numba"] = "0.60.0"
        del rebuilt["software"]["zstd"]
        self.assertEqual(
            [(d.field, d.original, d.rebuilt) for d in compare.failures(compare.differences(genotype_info(), rebuilt))],
            [("software.numba", compare.ABSENT, "0.60.0"), ("software.zstd", "1.5.6", compare.ABSENT)],
        )

    def test_only_the_closed_list_is_the_builds_own(self) -> None:
        rebuilt = self.rebuilt()
        rebuilt["software"] = {**rebuilt["software"], "ogld": {"version": "0.20.0"}}
        rebuilt["build"]["started_at"] = "2026-11-26T09:00:00Z"
        failed = [d.field for d in compare.failures(compare.differences(genotype_info(), rebuilt))]
        self.assertEqual(failed, ["build.started_at", "software.ogld"], "a nested software value is no version")

    def scanned(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """OGL-00009's provenance with its PC Check's first scan (the fixture is trimmed of it), twice."""
        scan = {
            "local": False, "pc": "PC1", "ratio": 0.6910857870381278, "rest_null_q99": 0.018983386228505088,
            "rest_share": 0.45717029205198967, "window": "16:6021628-10021627",
            "window_null_q99": 0.08842331315614795, "window_share": 0.6615246625333465,
        }
        original, rebuilt = genotype_info(), self.rebuilt()
        original["adjustment"]["pc_check"] = {"dropped": [], "scan": [dict(scan)]}
        rebuilt["adjustment"]["pc_check"] = {"dropped": [], "scan": [dict(scan)]}
        return original, rebuilt

    def test_a_scan_statistic_in_its_last_digits_passes_and_is_reported(self) -> None:
        """The acceptance run's 77 differences: each scan statistic about 1e-15 apart (fix3's ruling)."""
        original, rebuilt = self.scanned()
        rebuilt["adjustment"]["pc_check"]["scan"][0]["ratio"] = 0.6910857870381278 * (1 + 1e-15)
        rebuilt["adjustment"]["pc_check"]["scan"][0]["rest_null_q99"] = 0.01898338622850509
        found = compare.differences(original, rebuilt)
        self.assertEqual(compare.failures(found), [])
        tolerated = [d.field for d in found if d.within_tolerance]
        self.assertEqual(tolerated, ["adjustment.pc_check.scan[0].ratio", "adjustment.pc_check.scan[0].rest_null_q99"])
        self.assertIn(
            "adjustment.pc_check.scan[0].rest_null_q99: 0.018983386228505088 -> 0.01898338622850509 (within tolerance)",
            compare.render(found),
        )

    def test_each_tolerated_statistic_is_one_of_five(self) -> None:
        self.assertEqual(
            compare.TOLERATED_SCAN_STATISTICS,
            frozenset({"ratio", "rest_null_q99", "rest_share", "window_null_q99", "window_share"}),
        )
        self.assertEqual(compare.RELATIVE_TOLERANCE, 1e-9)
        for statistic in sorted(compare.TOLERATED_SCAN_STATISTICS):
            with self.subTest(statistic=statistic):
                original, rebuilt = self.scanned()
                rebuilt["adjustment"]["pc_check"]["scan"][0][statistic] *= 1 + 1e-12
                self.assertEqual(compare.failures(compare.differences(original, rebuilt)), [])

    def test_a_scan_statistic_beyond_the_tolerance_fails(self) -> None:
        original, rebuilt = self.scanned()
        rebuilt["adjustment"]["pc_check"]["scan"][0]["ratio"] = 0.6910857870381278 * (1 + 1e-6)
        self.assertEqual([d.field for d in compare.failures(compare.differences(original, rebuilt))],
                         ["adjustment.pc_check.scan[0].ratio"])

    def test_every_other_field_stays_exact(self) -> None:
        for field, change in (
            ("adjustment.pc_check.scan[0].pc", lambda s: s["scan"][0].update(pc="PC2")),
            ("adjustment.pc_check.scan[0].window", lambda s: s["scan"][0].update(window="16:6021629-10021628")),
            ("adjustment.pc_check.dropped", lambda s: s.update(dropped=["PC3"])),
            ("adjustment.pc_check.scan[0].ratio", lambda s: s["scan"][0].update(ratio="0.6910857870381278")),
        ):
            with self.subTest(field=field):
                original, rebuilt = self.scanned()
                change(rebuilt["adjustment"]["pc_check"])
                failed = [d.field for d in compare.failures(compare.differences(original, rebuilt))]
                self.assertEqual(failed, [field])
        original, rebuilt = self.scanned()
        rebuilt["t_far"] = original["t_far"] * (1 + 1e-15)
        self.assertEqual([d.field for d in compare.failures(compare.differences(original, rebuilt))], ["t_far"],
                         "a value the Store holds is compared exactly")
        original, rebuilt = self.scanned()
        del rebuilt["adjustment"]["pc_check"]["scan"][0]["ratio"]
        self.assertEqual([d.field for d in compare.failures(compare.differences(original, rebuilt))],
                         ["adjustment.pc_check.scan[0].ratio"], "a statistic one side lacks fails")

    def test_build_threads_is_not_the_builds_own(self) -> None:
        rebuilt = self.rebuilt()
        rebuilt["build"]["threads"] = 16
        self.assertEqual([d.field for d in compare.failures(compare.differences(genotype_info(), rebuilt))],
                         ["build.threads"])

    def test_every_difference_is_reported(self) -> None:
        text = compare.render(compare.differences(genotype_info(), self.rebuilt()))
        self.assertIn("build.created_at: '2026-11-21T20:37:12Z' -> '2026-11-26T09:14:03Z' (the build's own)", text)
        self.assertIn("software.ogld: '0.5.0' -> '0.20.0' (the build's own)", text)

    def accept(self, rebuilt: dict[str, Any], recorded_sha256: str | None):
        seen: list[list[str]] = []

        def info_runner(argv):
            seen.append(list(argv))
            payload = genotype_info() if argv[-1] == AMR_GENOTYPE_STORE else rebuilt
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        found, problems = compare.accept(
            Path(AMR_GENOTYPE_STORE), Path("/s/new.store"), recorded_sha256=recorded_sha256, info_runner=info_runner,
        )
        return seen, found, problems

    def test_the_acceptance_run_compares_through_the_cli_and_validates_nothing_again(self) -> None:
        seen, found, problems = self.accept(self.rebuilt(), provenance_sha256(genotype_info()))
        self.assertEqual(seen, [
            ["opengwasdb", "ld", "info", AMR_GENOTYPE_STORE],
            ["opengwasdb", "ld", "info", "/s/new.store"],
        ])
        self.assertEqual((compare.failures(found), problems), ([], []))

    def test_comparing_alone_still_checks_the_provenance(self) -> None:
        """`ld-stores compare` is `accept` without a record: a missing `build.created_at` fails both ways."""
        rebuilt = self.rebuilt()
        del rebuilt["build"]["created_at"]
        _, found, problems = self.accept(rebuilt, None)
        self.assertEqual([d.field for d in compare.failures(found)], ["build.created_at"])
        self.assertEqual(len(problems), 1)
        self.assertIn("build.created_at", problems[0])

    def test_an_original_that_is_not_its_records_store_fails(self) -> None:
        _, _, problems = self.accept(self.rebuilt(), "0" * 64)
        self.assertEqual(len(problems), 1)
        self.assertIn(AMR_GENOTYPE_STORE, problems[0])
        self.assertIn("0" * 64, problems[0])


#: The AMR LD Store's (OGL-00003) `pc_diagnostic`, from its `opengwasdb ld info`; the EUR fixture is trimmed of it.
PC_DIAGNOSTIC = {
    "pairs": {"source": "opengwas/opengwasdb#405's seeded cross-chromosome pairs", "seed": 405, "n_pairs": 200000},
    "rate_r2_ge_0_01_unadjusted": 0.120365,
    "rate_r2_ge_0_01_adjusted": 0.069475,
    "rate_r2_ge_0_01_sampling": 0.06927988322120199,
    "near_100kb_change": {"0": -0.38236518883158, "0.5": 4.76837158203125e-07, "0.9": 0.04143190383911133,
                          "0.99": 0.09489202499389648, "1": 0.3390759301146172},
}


class TestWhatTheProvenanceMustRecord(unittest.TestCase):
    """The checks of a Store's `ld info` that moved here from opengwasdb's `test_hgdp1kgp_stores.py`."""

    def ld_store(self) -> dict[str, Any]:
        info = json.loads((FIXTURES / "ld-info-eur.json").read_text(encoding="utf-8"))
        info["adjustment"]["pc_check"] = {"dropped": []}
        info["pc_diagnostic"] = copy.deepcopy(PC_DIAGNOSTIC)
        return info

    def test_the_published_stores_pass(self) -> None:
        self.assertEqual(compare.provenance_problems(self.ld_store(), keep_n=665), [])
        self.assertEqual(compare.provenance_problems(genotype_info(), keep_n=341), [])

    def test_n_must_be_the_keep_files(self) -> None:
        problems = compare.provenance_problems(genotype_info(), keep_n=340)
        self.assertEqual(len(problems), 1)
        self.assertIn("panel.n", problems[0])
        self.assertEqual(compare.provenance_problems(genotype_info(), keep_n=None), [])

    def test_each_check_names_its_field(self) -> None:
        for field, change in (
            ("panel.id", lambda i: i["panel"].update(id="hgdp1kgp-v2-EUR")),
            ("encoding.r_scale", lambda i: i["encoding"].update(r_scale=2047)),
            ("n_variants", lambda i: i.update(n_variants=0)),
            ("t_far", lambda i: i.update(t_far=1.5)),
            ("build.created_at", lambda i: i["build"].update(created_at="yesterday")),
            ("build.wall_seconds", lambda i: i["build"].update(wall_seconds=0)),
            ("build.peak_memory_bytes", lambda i: i["build"].pop("peak_memory_bytes")),
            ("software.ogld", lambda i: i["software"].update(ogld="")),
            ("adjustment.k_target", lambda i: i["adjustment"].update(k_target=15)),
            ("adjustment.k_min", lambda i: i["adjustment"].update(k_min=3)),
            ("adjustment.k", lambda i: i["adjustment"].update(k=4)),
            ("adjustment.covariates", lambda i: i["adjustment"].update(covariates=["PC1"])),
            ("adjustment.pc_check.dropped", lambda i: i["adjustment"]["pc_check"].update(dropped=None)),
        ):
            with self.subTest(field=field):
                info = genotype_info()
                change(info)
                problems = compare.provenance_problems(info, keep_n=341)
                self.assertTrue(problems, field)
                self.assertIn(field, problems[0])

    def test_an_ld_store_records_its_near_band_and_pc_diagnostic(self) -> None:
        for field, change in (
            ("counts.near_pairs", lambda i: i["counts"].update(near_pairs=0)),
            ("pc_diagnostic", lambda i: i.update(pc_diagnostic=None)),
            ("pc_diagnostic.pairs.n_pairs", lambda i: i["pc_diagnostic"]["pairs"].update(n_pairs=0)),
            ("pc_diagnostic.rate_r2_ge_0_01_adjusted", lambda i: i["pc_diagnostic"].update(rate_r2_ge_0_01_adjusted=float("nan"))),
            ("pc_diagnostic.near_100kb_change", lambda i: i["pc_diagnostic"]["near_100kb_change"].pop("0.99")),
        ):
            with self.subTest(field=field):
                info = self.ld_store()
                change(info)
                problems = compare.provenance_problems(info, keep_n=665)
                self.assertTrue(problems, field)
                self.assertIn(field, problems[0])

    def test_the_acceptance_run_checks_the_rebuilt_stores_provenance(self) -> None:
        rebuilt = genotype_info()
        rebuilt["build"]["created_at"] = "2026-11-26T09:14:03Z"

        def info_runner(argv):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rebuilt if argv[-1] == "/s/new.store" else genotype_info()), stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp, "AMR.keep")
            keep.write_text("#IID\n" + "".join(f"S{i}\n" for i in range(340)))
            _, problems = compare.accept(
                Path(AMR_GENOTYPE_STORE), Path("/s/new.store"), info_runner=info_runner, keep=keep,
            )
        self.assertEqual(len(problems), 1)
        self.assertIn("panel.n", problems[0])
        self.assertIn("340", problems[0])


class TestTheCommittedRecords(unittest.TestCase):
    """Which recorded Stores a rebuild can run: the Genotype Stores' `ogld build`, and OGL-00012's import (#542)."""

    def test_only_ogl_00007_to_00012_are_runnable(self) -> None:
        ld_root = REPO_ROOT / record.LD_ROOT
        runnable = []
        for path in sorted(ld_root.glob("OGL-*/record.yaml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            try:
                plan = rebuild.plan_rebuild(document, Path("/nonexistent/rebuild"))
            except rebuild.RebuildRefusal as refusal:
                self.assertIn("no runnable rebuild", str(refusal))
                continue
            runnable.append(document["ld_id"])
            if document["kind"] == "genotype_store":
                self.assertEqual(len(plan.copies), 22 * 3 + 2, "each chromosome's fileset, the keep and PC files")
            else:
                self.assertEqual(plan.copies, (), "an import reads its inputs where they are")
        self.assertEqual(runnable, ["OGL-00007", "OGL-00008", "OGL-00009", "OGL-00010", "OGL-00011", "OGL-00012"])


class TestTheRealLdStoresChecksums(unittest.TestCase):
    """OGL-00003's `opengwasdb ld info`, trimmed to `source`, `panel` and `adjustment.pc_file`
    (`fixtures/ld-info-amr-ld-store-checksums.json`, read from the Store on 25 November 2026)."""

    def provenance(self) -> dict[str, Any]:
        return json.loads((FIXTURES / "ld-info-amr-ld-store-checksums.json").read_text(encoding="utf-8"))

    def test_the_pc_file_is_not_in_source_sha256(self) -> None:
        provenance = self.provenance()
        recorded = provenance["source"]["sha256"]
        self.assertEqual(len(recorded), 67, "22 pgen, psam and pvar files, and the keep file")
        self.assertIn("AMR.keep", recorded)
        self.assertNotIn("AMR.eigenvec", recorded)
        self.assertEqual(provenance["adjustment"]["pc_file"]["sha256"],
                         "c70fe9f3f19bd71453ba31c0c42dd85cd992df1749650f96cb66da27056ed9f9")

    def test_plan_cs_staged_copies_each_have_a_checksum(self) -> None:
        plan = recipe.plan_build("genotype_store", "AMR", Path("/scratch/amr"), HOST)
        provenance = self.provenance()
        seen: list[list[str]] = []

        def info_runner(argv):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(provenance), stderr="")

        checksums = recipe.staged_checksums(plan, info_runner)
        self.assertEqual(seen, [["opengwasdb", "ld", "info", "/data/opengwasdb/work/ogld/stores/hgdp1kgp-v2-AMR-k10.store"]])
        self.assertEqual(sorted(checksums), sorted(target for _, target in plan.copies))
        self.assertEqual(checksums[Path("/scratch/amr/inputs/AMR.eigenvec")], provenance["adjustment"]["pc_file"]["sha256"])
        self.assertEqual(checksums[Path("/scratch/amr/inputs/AMR.keep")], provenance["source"]["sha256"]["AMR.keep"])
        self.assertEqual(checksums[Path("/scratch/amr/pgen/chr7.pvar")], provenance["source"]["sha256"]["chr7.pvar"])


class TestTheTool(unittest.TestCase):
    """`tools/ld-stores/ld_stores.py`, which a person runs, refuses before running the engine."""

    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "ld-stores" / "ld_stores.py"), *args],
            capture_output=True, text=True, check=False, cwd=REPO_ROOT,
        )

    def test_a_record_without_a_runnable_rebuild_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_tool("rebuild", "OGL-00001", "--out", f"{tmp}/eur")
            self.assertEqual(done.returncode, 1, done.stderr)
            self.assertIn("OGL-00001", done.stderr)
            self.assertIn("scripts/build_hgdp1kgp.py", done.stderr)
            self.assertFalse(Path(tmp, "eur").exists())

    def test_compare_holds_n_to_the_keep_file(self) -> None:
        """Plan C compares with `--keep`, so `panel.n` is held to the keep file. The engine is a stand-in
        on PATH that prints OGL-00009's `ld info` for either Store."""
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            (tmp / "info.json").write_text(json.dumps(genotype_info()), encoding="utf-8")
            engine = tmp / "bin" / "opengwasdb"
            engine.parent.mkdir()
            engine.write_text(f"#!/bin/sh\ncat {tmp / 'info.json'}\n")
            engine.chmod(0o755)
            for samples, status in ((341, 0), (340, 1)):
                with self.subTest(samples=samples):
                    keep = tmp / f"AMR-{samples}.keep"
                    keep.write_text("#IID\n" + "".join(f"S{i}\n" for i in range(samples)))
                    done = subprocess.run(
                        [sys.executable, str(REPO_ROOT / "tools" / "ld-stores" / "ld_stores.py"), "compare",
                         "/a/x.store", "/b/x.store", "--keep", str(keep)],
                        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
                        env={**os.environ, "PATH": f"{engine.parent}:{os.environ['PATH']}"},
                    )
                    self.assertEqual(done.returncode, status, done.stdout + done.stderr)
                    if status:
                        self.assertIn("panel.n", done.stderr)
                        self.assertIn("340", done.stderr)

    def test_build_refuses_an_unknown_panel(self) -> None:
        done = self.run_tool("build", "--kind", "ld_store", "--panel", "XYZ", "--out", "/nonexistent", "--ld-id", "OGL-00099")
        self.assertEqual(done.returncode, 2)
        self.assertIn("XYZ", done.stderr)


if __name__ == "__main__":
    unittest.main()
