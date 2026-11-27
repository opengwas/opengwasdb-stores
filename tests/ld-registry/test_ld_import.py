#!/usr/bin/env python3
"""The Pan-UKB import recipe, its re-plan and the plan comparison (#542, epic #496; docs/spec/ld-store-registry.md).

Five seams, each with a stub runner, so no test here runs the engine:

1. the ancestry gate: EUR alone, every other ancestry refused naming ogld#104;
2. the recipe's command line: it equals #10's real `ogld import` command line
   (`/data/opengwasdb/work/orchestrate/ogld-88/results/10/import-time.txt`),
   modernised to `opengwasdb ld import`;
3. `compare-plan`, on small literal plans;
4. an engine without `--plan-only`: a named refusal, from the CLI's own
   "No such option" error;
5. the full import's record: validate once, then the writer with a `rebuild`
   whose inputs the writer pins from the Store's own `source.sha256`.

Run from the repository root:
    python3 tests/ld-registry/test_ld_import.py
"""

from __future__ import annotations

import copy
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

from ld_registry import compare, importing, rebuild, recipe, record, writer
from ld_registry.recipe import RecipeRefusal

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOOL = REPO_ROOT / "tools" / "ld-stores" / "ld_stores.py"

#: #10's command line, as `/usr/bin/time -v` recorded it ("Command being timed") in
#: ogld-88/results/10/import-time.txt, run by ogld-88/scripts/run10.sh at ogld 0.8.0 and 0.8.1.
TEN_COMMAND = (
    "pixi run -e dev ogld import /data/opengwasdb/work/ogld/scratch/10/panukb-EUR-k15.store"
    " --work /data/opengwasdb/work/ogld/scratch/10/work"
    " --metadata /data/opengwasdb/work/ogld/scratch/88/downloads/bm-metadata.json"
    " --variants /data/opengwasdb/work/ogld/scratch/10/variants-autosomes.tsv.bgz"
    " --chain /data/opengwasdb/reference/liftover/hg19ToHg38.over.chain.gz"
    " --fasta /data/opengwasdb/work/ogld/scratch/10/downloads/hg38.fa.gz"
    " --parts /data/opengwasdb/work/ogld/scratch/88/blocks --delete-parts --download-workers 8 --threads 16"
)
TEN = {
    "out": Path("/data/opengwasdb/work/ogld/scratch/10/panukb-EUR-k15.store"),
    "work": Path("/data/opengwasdb/work/ogld/scratch/10/work"),
    "metadata": Path("/data/opengwasdb/work/ogld/scratch/88/downloads/bm-metadata.json"),
    "variants": Path("/data/opengwasdb/work/ogld/scratch/10/variants-autosomes.tsv.bgz"),
    "chain": Path("/data/opengwasdb/reference/liftover/hg19ToHg38.over.chain.gz"),
    "fasta": Path("/data/opengwasdb/work/ogld/scratch/10/downloads/hg38.fa.gz"),
}
#: OGL-00012's `opengwasdb ld info` `source.sha256` of its four inputs (read 25 November 2026);
#: they are also #10's plan identity (`results/10/plan-final.json`, `identity.inputs`).
OGL_00012_INPUTS = {
    "metadata": "80090e3ffe90add91d5f917d3cbd25b80ff125e6f76c973b8cce004b4e718d2d",
    "variants": "11b72c8b46ee3f5cb00b881acd8b25eb75f42b6b28f91d1c3028374e0e8ac1ad",
    "chain": "5c0598e500ceb5a78c73086929e8ef993aec309bcafb595139b53d440b125a1d",
    "fasta": "c1dd87068c254eb53d944f71e51d1311964fce8de24d6fc0effc9c61c01527d4",
}
ENVIRONMENT = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "PYTHONNOUSERSITE": "1"}
#: The locked engine's answer to `opengwasdb ld import --plan-only --help` (exit 2), as it printed it.
NO_SUCH_OPTION = (
    "Usage: opengwasdb ld import [OPTIONS] OUT\nTry 'opengwasdb ld import --help' for help.\n"
    "╭─ Error ─────────────────────────────────╮\n│ No such option '--plan-only'.           │\n"
    "╰─────────────────────────────────────────╯\n"
)
PROBE = ["opengwasdb", "ld", "import", "--plan-only", "--help"]


def ten_plan(**changes: Any) -> importing.ImportPlan:
    arguments = {**TEN, "parts": Path("/data/opengwasdb/work/ogld/scratch/88/blocks"), "delete_parts": True,
                 "download_workers": 8, "threads": 16, **changes}
    return importing.plan_import("EUR", **arguments)


def panukb_info() -> dict[str, Any]:
    """OGL-00012's `ld info` (the trimmed fixture), with its inputs' `source.sha256` and two parts'."""
    info = json.loads((FIXTURES / "ld-info-panukb-eur.json").read_text(encoding="utf-8"))
    info["source"]["sha256"] = {**OGL_00012_INPUTS, "part-0-0": "a" * 64, "part-0-1": "b" * 64}
    return info


class Helps:
    """A stub of the engine's answer to the probe `import --plan-only --help`: records each argv.
    By default the engine has the option (help, exit 0); `OLD` is the locked engine's refusal."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode, self.stderr, self.seen = returncode, stderr, []

    def __call__(self, argv):
        self.seen.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, stdout="Usage: ...\n", stderr=self.stderr)


def old_engine() -> Helps:
    return Helps(2, NO_SUCH_OPTION)


class Commands:
    """A stub runner: records each argv and its environment; an import makes its Store's directory,
    a plan-only one its work directory's plan."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, argv, environment=None) -> None:
        self.seen.append(list(argv))
        self.environments.append(dict(environment or {}))
        if list(argv[:3]) == ["opengwasdb", "ld", "import"]:
            work = Path(argv[argv.index("--work") + 1])
            work.mkdir(parents=True, exist_ok=True)
            (work / "plan.json").write_text("{}")
            if "--plan-only" not in argv:
                Path(argv[3]).mkdir(parents=True)


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.ld_root = self.tmp / "stores" / "ld"

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestTheEnginesPlanOnlyInputs(TempCase):
    """Base for tests that must reach the engine probe: the plan's inputs are small temporary files,
    since the real ones (TEN's paths) exist on the build host only and are refused first when absent."""

    def local_plan(self, **changes: Any) -> importing.ImportPlan:
        held = tempfile.TemporaryDirectory()  # apart from self.tmp, which some tests assert stays empty
        self.addCleanup(held.cleanup)
        root = Path(held.name)
        inputs: dict[str, Path] = {}
        for name in importing.INPUTS:
            inputs[name] = root / TEN[name].name
            inputs[name].write_text(name, encoding="utf-8")
        parts = root / "blocks"
        parts.mkdir()
        return ten_plan(**{**inputs, "parts": parts, **changes})


class TestTheAncestryGate(unittest.TestCase):
    """Seam 1: the engine's importer reads EUR alone until ogld#104."""

    def test_eur_is_accepted(self) -> None:
        importing.require_ancestry("EUR")
        self.assertEqual(ten_plan().store, TEN["out"])

    def test_every_other_ancestry_is_refused_naming_ogld_104(self) -> None:
        for ancestry in ("AFR", "AMR", "CSA", "EAS", "MID", "eur", ""):
            with self.subTest(ancestry=ancestry):
                with self.assertRaises(RecipeRefusal) as caught:
                    importing.plan_import(ancestry, **TEN)
                self.assertIn("ogld#104", str(caught.exception))
                self.assertIn(repr(ancestry), str(caught.exception))

    def test_the_tool_refuses_before_running_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(TOOL), "import", "--ancestry", "AFR", "--out", f"{tmp}/x.store",
                 "--work", f"{tmp}/work", *(f"--{name}={tmp}/{name}" for name in importing.INPUTS), "--plan-only"],
                capture_output=True, text=True, check=False, cwd=REPO_ROOT,
            )
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("ogld#104", done.stderr)
            self.assertEqual(sorted(Path(tmp).iterdir()), [])


class TestTheRecipesCommandLine(unittest.TestCase):
    """Seam 2: #10's command line, modernised."""

    def test_it_is_tens_command_modernised(self) -> None:
        expected = shlex.split(TEN_COMMAND.replace("pixi run -e dev ogld", "opengwasdb ld"))
        self.assertEqual(list(ten_plan().command), expected)

    def test_a_full_import_validates_once_at_its_threads(self) -> None:
        plan = ten_plan()
        self.assertEqual(plan.commands, (plan.command, ("opengwasdb", "ld", "validate", "--threads", "16", str(TEN["out"]))))

    def test_a_plan_only_run_adds_the_option_and_validates_nothing(self) -> None:
        plan = ten_plan(plan_only=True)
        expected = shlex.split(TEN_COMMAND.replace("pixi run -e dev ogld", "opengwasdb ld")) + ["--plan-only"]
        self.assertEqual(plan.commands, (tuple(expected),))

    def test_options_not_given_are_the_engines_defaults(self) -> None:
        plan = importing.plan_import("EUR", **TEN)
        self.assertEqual(list(plan.command), [
            "opengwasdb", "ld", "import", str(TEN["out"]), "--work", str(TEN["work"]),
            *(item for name in importing.INPUTS for item in (f"--{name}", str(TEN[name]))),
        ])
        self.assertEqual(plan.validate, ("opengwasdb", "ld", "validate", str(TEN["out"])))

    def test_the_rebuild_it_records_runs_its_commands_and_names_its_inputs(self) -> None:
        """The recipe's one `rebuild` section, with the inputs' paths; the writer pins their digests."""
        plan = ten_plan()
        self.assertEqual(importing.rebuild_section(plan, "v0.48.0", "dev"), {
            "repository": "opengwas/opengwasdb-stores",
            "version": "v0.48.0",
            "script": "opengwasdb ld import",
            "commands": [shlex.join(["pixi", "run", "-e", "dev", *command]) for command in plan.commands],
            "inputs": {name: {"path": str(TEN[name])} for name in importing.INPUTS},
        })
        self.assertEqual(importing.rebuild_section(plan, "v0.48.0", "dev"),
                         recipe.rebuild_section(plan.commands, "v0.48.0", "dev", "opengwasdb ld import",
                                                inputs={n: {"path": str(TEN[n])} for n in importing.INPUTS}))


class TestTheEnginesPlanOnly(TestTheEnginesPlanOnlyInputs):
    """Seam 4: the engine is probed with `import --plan-only --help`; its own "No such option" is a named refusal."""

    def test_an_engine_without_it_is_refused_naming_the_change(self) -> None:
        helps, commands = old_engine(), Commands()
        plan = self.local_plan(out=self.tmp / "x.store", work=self.tmp / "work", plan_only=True)
        with self.assertRaises(RecipeRefusal) as caught:
            importing.plan_store(plan, run=commands, help_runner=helps)
        self.assertIn("--plan-only", str(caught.exception))
        self.assertIn(recipe.PLAN_ONLY_CHANGE, str(caught.exception))
        self.assertEqual(helps.seen, [PROBE])
        self.assertEqual(commands.seen, [])
        self.assertEqual(sorted(self.tmp.iterdir()), [])

    def test_a_probe_that_fails_otherwise_is_refused_with_its_error(self) -> None:
        plan = self.local_plan(out=self.tmp / "x.store", work=self.tmp / "work", plan_only=True)
        with self.assertRaises(RecipeRefusal) as caught:
            importing.plan_store(plan, run=Commands(), help_runner=Helps(1, "ImportError: zarr"))
        self.assertIn(shlex.join(PROBE), str(caught.exception))
        self.assertIn("ImportError: zarr", str(caught.exception))
        self.assertNotIn(recipe.PLAN_ONLY_CHANGE, str(caught.exception), "not mistaken for a missing option")

    def test_an_engine_that_is_not_there_is_refused(self) -> None:
        def missing(argv):
            raise FileNotFoundError(2, "No such file or directory", "opengwasdb")

        plan = self.local_plan(out=self.tmp / "x.store", work=self.tmp / "work", plan_only=True)
        with self.assertRaises(RecipeRefusal) as caught:
            importing.plan_store(plan, run=Commands(), help_runner=missing)
        self.assertIn(shlex.join(PROBE), str(caught.exception))
        self.assertIn("No such file", str(caught.exception))

    def test_another_missing_option_is_not_plan_only(self) -> None:
        plan = self.local_plan(out=self.tmp / "x.store", work=self.tmp / "work", plan_only=True)
        with self.assertRaises(RecipeRefusal) as caught:
            importing.plan_store(plan, run=Commands(), help_runner=Helps(2, "No such option '--help'."))
        self.assertNotIn(recipe.PLAN_ONLY_CHANGE, str(caught.exception))

    def test_an_engine_with_it_runs_the_plan_only_import_and_writes_no_record(self) -> None:
        helps, commands = Helps(), Commands()
        out = self.tmp / "replan" / "panukb-EUR-k15.store"
        plan = self.local_plan(out=out, work=self.tmp / "replan" / "work", plan_only=True)
        written = importing.plan_store(plan, run=commands, help_runner=helps)
        self.assertEqual(written, self.tmp / "replan" / "work" / "plan.json")
        self.assertEqual(commands.seen, [list(plan.command)])
        self.assertEqual(commands.environments, [{**ENVIRONMENT, "TMPDIR": f"{out.parent}/.{out.name}.tmp"}])
        self.assertFalse(out.exists())
        self.assertFalse(self.ld_root.exists())

    def test_a_plan_only_run_refuses_an_output_that_exists_or_a_missing_input(self) -> None:
        out = self.tmp / "x.store"
        out.mkdir()
        for name, plan, needle in (
            ("an output that exists", self.local_plan(out=out, work=self.tmp / "w", plan_only=True), str(out)),
            ("a missing input", self.local_plan(out=self.tmp / "y.store", work=self.tmp / "w", plan_only=True,
                                         variants=self.tmp / "none.tsv.bgz"), "none.tsv.bgz"),
        ):
            with self.subTest(name):
                commands, helps = Commands(), Helps()
                with self.assertRaises(RecipeRefusal) as caught:
                    importing.plan_store(plan, run=commands, help_runner=helps)
                self.assertIn(needle, str(caught.exception))
                self.assertEqual((commands.seen, helps.seen), ([], []))

    def test_a_full_plan_is_not_a_plan_only_run(self) -> None:
        with self.assertRaises(ValueError):
            importing.plan_store(self.local_plan(out=self.tmp / "x.store"), run=Commands(), help_runner=Helps())


class TestTheFullImport(TempCase):
    """Seam 5: import, validate once, then the writer with the import's `rebuild`."""

    def inputs(self) -> dict[str, Path]:
        paths = {}
        for name in importing.INPUTS:
            paths[name] = self.tmp / "in" / name
            paths[name].parent.mkdir(parents=True, exist_ok=True)
            paths[name].write_text(name)
        return paths

    def run_import(self, plan: importing.ImportPlan, commands: Commands, info: dict[str, Any] | None = None):
        def info_runner(argv):
            commands.seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(info or panukb_info()), stderr="")

        return importing.import_store(plan, self.ld_root, "OGL-00099", run=commands, info_runner=info_runner,
                                      version="v0.48.0", environment="dev", size=lambda _: 1)

    def test_it_imports_validates_once_and_writes_the_record(self) -> None:
        plan = importing.plan_import("EUR", out=self.tmp / "out" / "panukb-EUR-k15.store", work=self.tmp / "out" / "work",
                                     threads=16, **self.inputs())
        commands = Commands()
        self.run_import(plan, commands)
        info = ["opengwasdb", "ld", "info", str(plan.store)]
        self.assertEqual(commands.seen, [list(plan.command), list(plan.validate), info],
                         "the writer reads the Store once, its source.sha256 included")
        document, errors = record.load_record(record.record_path(self.ld_root, "OGL-00099"))
        section = importing.rebuild_section(plan, "v0.48.0", "dev")
        for name, entry in section["inputs"].items():
            entry["sha256"] = OGL_00012_INPUTS[name]
        self.assertEqual(document["rebuild"], section, "the writer pins each input's digest from source.sha256")
        self.assertEqual(document["provenance"]["source_panel"], "Pan-UKB-EUR")
        self.assertEqual(sorted({e.field for e in errors}), ["label", "licence", "status", "used_for"],
                         "a person writes these")

    def test_a_store_whose_source_does_not_pin_an_input_is_refused(self) -> None:
        info = panukb_info()
        del info["source"]["sha256"]["fasta"]
        plan = importing.plan_import("EUR", out=self.tmp / "o" / "x.store", work=self.tmp / "o" / "w", **self.inputs())
        with self.assertRaises(writer.WriterRefusal) as caught:
            self.run_import(plan, Commands(), info)
        self.assertIn("rebuild.inputs.fasta.sha256", str(caught.exception))
        self.assertFalse(record.record_path(self.ld_root, "OGL-00099").exists())

    def test_it_refuses_before_running_anything(self) -> None:
        inputs = self.inputs()
        exists = self.tmp / "exists.store"
        exists.mkdir()
        path = record.record_path(self.ld_root, "OGL-00099")
        path.parent.mkdir(parents=True)
        cases = {
            "an output that exists": (importing.plan_import("EUR", out=exists, work=self.tmp / "w", **inputs), "exists"),
            "a missing input": (importing.plan_import(
                "EUR", out=self.tmp / "a.store", work=self.tmp / "w", **{**inputs, "chain": self.tmp / "none"}), "none"),
            "a plan-only plan": (importing.plan_import(
                "EUR", out=self.tmp / "b.store", work=self.tmp / "w", plan_only=True, **inputs), "plan-only"),
        }
        for name, (plan, needle) in cases.items():
            with self.subTest(name):
                commands = Commands()
                with self.assertRaises((RecipeRefusal, ValueError)) as caught:
                    self.run_import(plan, commands)
                self.assertIn(needle, str(caught.exception))
                self.assertEqual(commands.seen, [])
        path.write_text(yaml.safe_dump({"ld_id": "OGL-00099", "location": "/elsewhere.store"}))
        commands = Commands()
        with self.assertRaises(RecipeRefusal) as caught:
            self.run_import(importing.plan_import("EUR", out=self.tmp / "c.store", work=self.tmp / "w", **inputs), commands)
        self.assertIn("/elsewhere.store", str(caught.exception))
        self.assertEqual(commands.seen, [])


    def test_an_invalid_ld_id_is_refused_before_the_import_starts(self) -> None:
        plan = importing.plan_import("EUR", out=self.tmp / "o" / "x.store", work=self.tmp / "o" / "w", **self.inputs())
        commands = Commands()
        with self.assertRaises(RecipeRefusal) as caught:
            importing.import_store(plan, self.ld_root, "OGL-1", run=commands, version="v0.48.0", environment="dev",
                                   size=lambda _: 1)
        self.assertIn("OGL-1", str(caught.exception))
        self.assertEqual(commands.seen, [], "the import never starts")

class TestTheWritersInputs(TempCase):
    """The writer, the one reader of the Store's `source.sha256`, pins each `rebuild.inputs` digest from it;
    a digest given must be that one, and each input's path must be the one the commands read."""

    def write(self, inputs: dict[str, Any], command: str | None = None):
        flags = "".join(f" --{name} {entry['path']}" for name, entry in inputs.items())
        section = {"repository": "opengwas/opengwasdb-stores", "version": "v0.48.0", "script": "opengwasdb ld import",
                   "commands": [command or f"opengwasdb ld import /x.store --work /w{flags}"], "inputs": inputs}

        def info_runner(argv):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(panukb_info()), stderr="")

        return writer.write_record(self.ld_root, "OGL-00099", self.tmp / "x.store", runner=info_runner,
                                   size=lambda _: 1, measure_links=lambda _: {}, rebuild=section)

    def written_inputs(self) -> dict[str, Any]:
        path = record.record_path(self.ld_root, "OGL-00099")
        return yaml.safe_load(path.read_text(encoding="utf-8"))["rebuild"]["inputs"]

    def test_the_stores_own_digests_are_written(self) -> None:
        self.write({name: {"path": f"/in/{name}", "sha256": digest} for name, digest in OGL_00012_INPUTS.items()})
        self.assertEqual(self.written_inputs()["fasta"], {"path": "/in/fasta", "sha256": OGL_00012_INPUTS["fasta"]})

    def test_inputs_given_by_path_alone_are_pinned_from_source_sha256(self) -> None:
        self.write({name: {"path": f"/in/{name}"} for name in importing.INPUTS})
        self.assertEqual(self.written_inputs(),
                         {name: {"path": f"/in/{name}", "sha256": OGL_00012_INPUTS[name]} for name in importing.INPUTS})

    def test_an_input_written_with_equals_is_read(self) -> None:
        inputs = {name: {"path": f"/in/{name}"} for name in importing.INPUTS}
        self.write(inputs, "opengwasdb ld import /x.store --work /w" + "".join(
            f" --{name}={entry['path']}" for name, entry in inputs.items()))
        self.assertEqual(self.written_inputs()["variants"]["sha256"], OGL_00012_INPUTS["variants"])

    def test_an_input_path_the_commands_do_not_read_is_refused_naming_it(self) -> None:
        inputs = {name: {"path": f"/in/{name}"} for name in importing.INPUTS}
        for label, command in (
            ("another path", "opengwasdb ld import /x.store --work /w --metadata /in/metadata --variants /elsewhere"
                             " --chain /in/chain --fasta /in/fasta"),
            ("never read", "opengwasdb ld import /x.store --work /w --metadata /in/metadata --chain /in/chain"
                           " --fasta /in/fasta"),
        ):
            with self.subTest(label):
                with self.assertRaises(writer.WriterRefusal) as caught:
                    self.write(inputs, command)
                self.assertIn("rebuild.inputs.variants.path", str(caught.exception))
                self.assertFalse(record.record_path(self.ld_root, "OGL-00099").exists())

    def test_a_digest_the_store_did_not_read_is_refused_naming_it(self) -> None:
        for name, digest in (("variants", "0" * 64), ("ledger", "1" * 64)):
            with self.subTest(name):
                inputs = {n: {"path": f"/in/{n}", "sha256": d} for n, d in OGL_00012_INPUTS.items()}
                inputs[name] = {"path": f"/in/{name}", "sha256": digest}
                with self.assertRaises(writer.WriterRefusal) as caught:
                    self.write(inputs)
                self.assertIn(f"rebuild.inputs.{name}.sha256", str(caught.exception))
                self.assertFalse(record.record_path(self.ld_root, "OGL-00099").exists())

    def test_a_malformed_input_is_a_record_error(self) -> None:
        for value in ({"path": "relative", "sha256": "a" * 64}, {"path": "/x", "sha256": "A" * 64},
                      {"path": "/x"}, {"path": "/x", "sha256": "a" * 64, "size": 1}, "text"):
            with self.subTest(value=value):
                errors = record.validate({"rebuild": {"inputs": {"variants": value}}}, Path("OGL-00099/record.yaml"))
                self.assertIn("rebuild.inputs", {e.field for e in errors})


def plan(**chromosomes: dict[str, Any]) -> dict[str, Any]:
    """A small literal plan in the engine's shape: identity, chromosomes, the Store's far_bp."""
    return {
        "identity": {"inputs": dict(OGL_00012_INPUTS), "ledger_sha256": None, "out": "/run/a.store",
                     "far_target_bp": 8000000, "displaced_cap": 10000, "schema": 2},
        "chromosomes": chromosomes,
        "far_bp": min(entry["far_bp"] for entry in chromosomes.values()),
    }


def chromosome(far_bp: int, invalid_frequency: int = 0) -> dict[str, Any]:
    return {"parts": ["part-0-0", "part-0-1"], "far_bp": far_bp, "source_variants": 100,
            "losses": {"allele_mismatch": 2, "invalid_frequency": invalid_frequency, "liftover_displaced": 0}}


class TestComparingPlans(unittest.TestCase):
    """Seam 3: a plan against a reference plan, chromosome by chromosome."""

    def reference(self) -> dict[str, Any]:
        return plan(**{"15": chromosome(9143750), "22": chromosome(9591521)})

    def test_identical_plans_pass(self) -> None:
        self.assertEqual(compare.plan_differences(self.reference(), self.reference()), [])

    def test_another_output_is_the_runs_own_and_reported(self) -> None:
        replanned = self.reference()
        replanned["identity"]["out"] = "/run/b.store"
        found = compare.plan_differences(replanned, self.reference())
        self.assertEqual([d.field for d in found], ["identity.out"])
        self.assertEqual(compare.plan_failures(found), [])
        self.assertIn("(the run's own)", compare.render_plan(found))

    def test_one_changed_losses_count_fails_naming_the_chromosome_and_field(self) -> None:
        replanned = plan(**{"15": chromosome(9143750, invalid_frequency=3), "22": chromosome(9591521)})
        failed = compare.plan_failures(compare.plan_differences(replanned, self.reference()))
        self.assertEqual(failed, [compare.Difference("chromosomes.15.losses.invalid_frequency", 0, 3)])

    def test_an_extra_or_missing_chromosome_fails(self) -> None:
        extra = plan(**{"15": chromosome(9143750), "22": chromosome(9591521), "23": chromosome(9900000)})
        missing = plan(**{"15": chromosome(9143750)})
        self.assertEqual([d.field for d in compare.plan_failures(compare.plan_differences(extra, self.reference()))],
                         ["chromosomes.23"])
        failed = compare.plan_failures(compare.plan_differences(missing, self.reference()))
        self.assertEqual([d.field for d in failed], ["chromosomes.22"], "chr15 still holds the smallest far_bp")
        self.assertIs(failed[0].rebuilt, compare.ABSENT, "the plan lacks it")

    def test_each_compared_field_fails(self) -> None:
        for field, change in (
            ("chromosomes.22.parts[1]", lambda p: p["chromosomes"]["22"]["parts"].__setitem__(1, "part-9-9")),
            ("chromosomes.22.far_bp", lambda p: p["chromosomes"]["22"].__setitem__("far_bp", 9591520)),
            ("chromosomes.22.source_variants", lambda p: p["chromosomes"]["22"].__setitem__("source_variants", 99)),
            ("far_bp", lambda p: p.__setitem__("far_bp", 1)),
            ("identity.inputs.variants", lambda p: p["identity"]["inputs"].__setitem__("variants", "0" * 64)),
            ("identity.displaced_cap", lambda p: p["identity"].__setitem__("displaced_cap", 1)),
        ):
            with self.subTest(field):
                replanned = copy.deepcopy(self.reference())
                change(replanned)
                failed = compare.plan_failures(compare.plan_differences(replanned, self.reference()))
                self.assertEqual([d.field for d in failed], [field])

    def test_the_tool_exits_1_on_a_difference_and_names_it(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            (tmp / "reference.json").write_text(json.dumps(self.reference()))
            for label, replanned, status in (
                ("same", self.reference(), 0),
                ("changed", plan(**{"15": chromosome(9143750, 3), "22": chromosome(9591521)}), 1),
            ):
                with self.subTest(label):
                    (tmp / "plan.json").write_text(json.dumps(replanned))
                    done = subprocess.run([sys.executable, str(TOOL), "compare-plan", str(tmp / "plan.json"),
                                           str(tmp / "reference.json")], capture_output=True, text=True, check=False,
                                          cwd=REPO_ROOT)
                    self.assertEqual(done.returncode, status, done.stdout + done.stderr)
                    if status:
                        self.assertIn("chromosomes.15.losses.invalid_frequency", done.stderr)


class TestRebuildingAnImport(TempCase):
    """OGL-00012's recorded import: its rebuild and its plan-only re-plan."""

    def records(self, document: dict[str, Any]) -> None:
        path = record.record_path(self.ld_root, document["ld_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def committed(self) -> dict[str, Any]:
        path = REPO_ROOT / record.LD_ROOT / "OGL-00012" / record.RECORD_FILENAME
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_ogl_00012_records_its_inputs_as_its_store_read_them(self) -> None:
        inputs = self.committed()["rebuild"]["inputs"]
        self.assertEqual({name: entry["sha256"] for name, entry in inputs.items()}, OGL_00012_INPUTS)
        self.assertEqual({name: Path(entry["path"]) for name, entry in inputs.items()},
                         {name: TEN[name] for name in importing.INPUTS})

    def test_its_commands_are_tens_at_the_stores_location(self) -> None:
        """#10's own `ogld import` and `ogld validate`, its Store and work directory moved to the location."""
        document = self.committed()
        location = document["location"]
        command = TEN_COMMAND.replace(str(TEN["out"]), location).replace(
            str(TEN["work"]), str(Path(location).parent / "work"))
        self.assertEqual(document["rebuild"]["commands"],
                         [command, f"pixi run -e dev ogld validate --threads 16 {location}"])

    def test_its_repository_version_and_script_are_what_ran(self) -> None:
        section = self.committed()["rebuild"]
        self.assertEqual((section["repository"], section["script"]), ("opengwas/ogld", "ogld import"))
        self.assertIn("0.8.0", section["version"])
        self.assertIn("0.8.1", section["version"])
        notes = self.committed()["notes"]
        self.assertIn("/data/opengwasdb/work/ogld/scratch/10/panukb-EUR-k15.store", notes, "the relocation is disclosed")

    def test_a_plan_only_rebuild_runs_its_import_with_the_option_into_the_new_directory(self) -> None:
        document = self.records_with_local_inputs()
        out = self.tmp / "replan"
        commands, helps = Commands(), Helps()
        rebuilt = rebuild.rebuild_store(self.ld_root, "OGL-00012", out, run=commands, copy=self.fail_copy,
                                        plan_only=True, help_runner=helps)
        recorded = shlex.split(document["rebuild"]["commands"][0])
        self.assertEqual(recorded[:6], ["pixi", "run", "-e", "dev", "ogld", "import"])
        expected = [str(out / "panukb-EUR-k15.store") if a == document["location"]
                    else str(out / "work") if a == str(Path(document["location"]).parent / "work") else a
                    for a in recorded[6:]]
        self.assertEqual(commands.seen, [["opengwasdb", "ld", "import", *expected, "--plan-only"]])
        self.assertEqual(rebuilt.copies, ())
        self.assertEqual(rebuilt.work, out / "work")
        self.assertEqual(importing.plan_path(rebuilt.work), out / "work" / "plan.json")
        self.assertEqual(helps.seen, [PROBE])
        self.assertFalse((out / "panukb-EUR-k15.store").exists())

    def fail_copy(self, *args, **kwargs):
        raise AssertionError("an import copies nothing: its work directory is an output")

    def records_with_local_inputs(self) -> dict[str, Any]:
        """OGL-00012's committed record, its `rebuild` inputs moved to small temporary files (the real ones
        are on the build host only), recorded in the temporary registry; returns the moved record."""
        document = self.committed()
        moved: dict[str, str] = {}
        for name, entry in document["rebuild"]["inputs"].items():
            local = self.tmp / "inputs" / Path(entry["path"]).name
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(name, encoding="utf-8")
            moved[entry["path"]] = str(local)
        text = yaml.safe_dump(document["rebuild"], sort_keys=False)
        for original, local in moved.items():
            text = text.replace(original, local)
        rebuild_section = yaml.safe_load(text)
        document = {**document, "rebuild": rebuild_section}
        self.records({key: document[key] for key in ("ld_id", "kind", "rebuild", "location")})
        return document

    def test_a_full_rebuild_runs_the_import_then_its_validate(self) -> None:
        document = self.records_with_local_inputs()
        out = self.tmp / "full"
        commands = Commands()
        rebuilt = rebuild.rebuild_store(self.ld_root, "OGL-00012", out, run=commands, copy=self.fail_copy)
        self.assertEqual([argv[:3] for argv in commands.seen],
                         [["opengwasdb", "ld", "import"], ["opengwasdb", "ld", "validate"]])
        self.assertEqual(commands.seen[1][-1], str(out / "panukb-EUR-k15.store"))
        self.assertIsNone(rebuilt.keep)

    def test_a_plan_only_rebuild_on_an_old_engine_is_refused_before_anything_runs(self) -> None:
        document = self.records_with_local_inputs()
        commands = Commands()
        with self.assertRaises(RecipeRefusal) as caught:
            rebuild.rebuild_store(self.ld_root, "OGL-00012", self.tmp / "r", run=commands, plan_only=True,
                                  help_runner=old_engine())
        self.assertIn(recipe.PLAN_ONLY_CHANGE, str(caught.exception))
        self.assertEqual(commands.seen, [])
        self.assertFalse((self.tmp / "r").exists())

    def import_record(self, work: str, equals: bool = False) -> dict[str, Any]:
        """A small import record at /s/d/x.store whose work directory is `work`."""
        option = f"--work={work}" if equals else f"--work {work}"
        return {"ld_id": "OGL-00099", "kind": "ld_store", "location": "/s/d/x.store", "rebuild": {
            "repository": "opengwas/opengwasdb-stores", "version": "v1", "script": "opengwasdb ld import",
            "commands": [f"opengwasdb ld import /s/d/x.store {option} --metadata /in/m --variants /in/v"
                         " --chain /in/c --fasta /in/f --threads=4"]}}

    def test_a_work_directory_outside_the_stores_directory_is_refused_naming_it(self) -> None:
        """It would not move with the Store: a re-plan would write into the original work directory."""
        for equals in (False, True):
            for plan_only in (False, True):
                with self.subTest(equals=equals, plan_only=plan_only):
                    with self.assertRaises(rebuild.RebuildRefusal) as caught:
                        rebuild.plan_rebuild(self.import_record("/elsewhere/work", equals), self.tmp / "r",
                                             plan_only=plan_only)
                    self.assertIn("/elsewhere/work", str(caught.exception))
                    self.assertIn("OGL-00099", str(caught.exception))

    def test_an_option_written_with_equals_is_read_and_moved_as_one_written_apart(self) -> None:
        out = self.tmp / "r"
        planned = rebuild.plan_rebuild(self.import_record("/s/d/work", equals=True), out, plan_only=True)
        self.assertIn(f"--work={out}/work", planned.commands[0])
        self.assertEqual(planned.work, out / "work")
        full = rebuild.plan_rebuild(self.import_record("/s/d/work", equals=True), out)
        self.assertEqual(full.commands[-1], ("opengwasdb", "ld", "validate", "--threads", "4", str(out / "x.store")),
                         "the build's `--threads=4` sets the validation's")

    def test_a_plan_only_rebuild_of_a_build_is_refused(self) -> None:
        document = {"ld_id": "OGL-00009", "kind": "genotype_store", "location": "/s/d/x.store", "rebuild": {
            "repository": "opengwas/ogld", "version": "0.5.0", "script": "ogld build",
            "commands": ["ogld build /s/d/x.store --threads 8"]}}
        with self.assertRaises(rebuild.RebuildRefusal) as caught:
            rebuild.plan_rebuild(document, self.tmp / "r", plan_only=True)
        self.assertIn("--plan-only", str(caught.exception))


class TestTheToolsPlanOnlyImport(unittest.TestCase):
    """`ld_stores.py import --plan-only` through the tool: the engine's plan, and no record. The engine is a
    stand-in on PATH that answers the probe and writes a plan where `--work` says."""

    ENGINE = """#!/bin/sh
case " $* " in *" --help "*) echo "Usage: opengwasdb ld import"; exit 0;; esac
while [ $# -gt 0 ]; do if [ "$1" = --work ]; then mkdir -p "$2"; echo '{}' > "$2/plan.json"; fi; shift; done
"""

    def test_it_writes_the_plan_and_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            engine = tmp / "bin" / "opengwasdb"
            engine.parent.mkdir()
            engine.write_text(self.ENGINE)
            engine.chmod(0o755)
            inputs = []
            for name_ in importing.INPUTS:
                (tmp / name_).write_text(name_)
                inputs.append(f"--{name_}={tmp / name_}")
            ld_root = tmp / "stores" / "ld"
            ld_root.mkdir(parents=True)
            done = subprocess.run(
                [sys.executable, str(TOOL), "import", "--ancestry", "EUR", *inputs, "--work", str(tmp / "r" / "work"),
                 "--out", str(tmp / "r" / "x.store"), "--ld-root", str(ld_root), "--plan-only"],
                capture_output=True, text=True, check=False, cwd=REPO_ROOT,
                env={**os.environ, "PATH": f"{engine.parent}:{os.environ['PATH']}"},
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn(f"planned {tmp / 'r' / 'work' / 'plan.json'}", done.stdout)
            self.assertTrue((tmp / "r" / "work" / "plan.json").exists())
            self.assertEqual(list(ld_root.iterdir()), [], "a plan-only import writes no record")
            self.assertFalse((tmp / "r" / "x.store").exists())


class TestAcceptingAnImport(unittest.TestCase):
    """An imported Store's acceptance is the field-by-field comparison alone: what an HGDP+1kGP Store
    must record (its panel id, r_scale 511, PC Check) is not an imported matrix's."""

    def test_identical_imports_pass_with_no_problem(self) -> None:
        info = panukb_info()

        def runner(argv):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(info), stderr="")

        self.assertEqual(compare.accept(Path("/a.store"), Path("/b.store"), info_runner=runner), ([], []))

    def test_an_hgdp1kgp_store_is_still_checked(self) -> None:
        info = json.loads((FIXTURES / "ld-info-eur.json").read_text(encoding="utf-8"))
        info["encoding"]["r_scale"] = 2047

        def runner(argv):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(info), stderr="")

        _, problems = compare.accept(Path("/a.store"), Path("/b.store"), info_runner=runner)
        self.assertTrue(any("encoding.r_scale" in problem for problem in problems), problems)


if __name__ == "__main__":
    unittest.main()
