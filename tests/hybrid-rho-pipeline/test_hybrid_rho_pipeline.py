#!/usr/bin/env python3
"""A Hybrid Store through build-hybrid -> rho -> validate -> register, with the real engine (#529).

The one smoke test of `post.rho` on a Hybrid against the engine (`build-dense-rho` on the
Hybrid release, opengwasdb#496); what the Rho Matrix holds is the engine's to test. The
fixture is `clumps-pipeline`'s: two GWAS-VCF Analyses over the Test Panel's 911 SNV rows
(`a` at the panel's z, `b` at 0.9 z), in the shared fixture bundle with its recipe turned
to `build-hybrid`. A plain ALID variant reference leaves out the last `OFF` rows, so they
go to Ragged Overflow and the Dense Component holds the rest.

`register` writes `validation.yaml`, which must carry `has_rho: true`.

Run from the repository root:
    pixi run -e dev python3 tests/hybrid-rho-pipeline/test_hybrid_rho_pipeline.py
"""

from __future__ import annotations

import csv
import gzip
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "workflow"))

from test_workflow import (  # noqa: E402
    REPO_ROOT,
    create_dense_fixture_store,
    prepare_hybrid_single_id_signal_release,
    release_argv,
)

from ogstores import bundle, manifest, paths, run  # noqa: E402
from ogstores.plan import plan  # noqa: E402
from ogstores.register import register_release  # noqa: E402

FIXTURES = HERE.parent / "clumps-pipeline" / "fixtures"
STORE_ID = "OGS-99529"
ANALYSES = (("a", 1.0), ("b", 0.9))
OFF = 5
VCF_HEADER = (
    "##fileformat=VCFv4.2\n"
    '##FORMAT=<ID=EZ,Number=A,Type=Float,Description="Z-score">\n'
    '##FORMAT=<ID=ES,Number=A,Type=Float,Description="Effect size">\n'
    '##FORMAT=<ID=SE,Number=A,Type=Float,Description="Standard error">\n'
    '##FORMAT=<ID=AF,Number=A,Type=Float,Description="Allele frequency">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRAIT\n"
)


class HybridRhoPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.td = Path(tempfile.mkdtemp(prefix="hybrid-rho-pipeline-"))
        cls.addClassCleanup(shutil.rmtree, cls.td, ignore_errors=True)
        cls.registry, cls.root = cls.td / "stores", cls.td / "artifacts"
        cls.bundle = cls.make_bundle()
        cls.steps = plan(cls.bundle, artifact_root=cls.root)
        for step in cls.steps:
            run.execute_step(step, bundle=cls.bundle, artifact_root=cls.root)
        cls.observed = register_release(
            cls.bundle, registry_root=cls.registry, artifact_root=cls.root
        )["observed"]

    @classmethod
    def make_bundle(cls) -> bundle.Bundle:
        directory = create_dense_fixture_store(
            cls.registry, STORE_ID, artifact_root=cls.root, post_top_hits=False,
            post_overview=False, post_rho=True, trial=True,
        )
        (directory / "test.vcf.gz").unlink()
        with open(FIXTURES / "rows.tsv", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        reference = directory / "variant-reference.txt"
        reference.write_text(
            "".join(f"{r['chrom']}:{r['pos']}:{r['ref']}:{r['alt']}\n" for r in rows[:-OFF]),
            encoding="utf-8",
        )
        build_yaml_p = directory / "build.yaml"
        recipe = yaml.safe_load(build_yaml_p.read_text(encoding="utf-8"))
        recipe["layout"] = "hybrid"
        recipe["build"]["command"] = "build-hybrid"
        recipe["build"]["options"]["variant-reference"] = str(reference)
        build_yaml_p.write_text(yaml.safe_dump(recipe), encoding="utf-8")
        lines = ["analysis_id\tsource_file\ttrait_name\tsample_size\tstored_effect_scale\toriginal_sd_method"
                 "\toriginal_sd\tassigned_ancestry\tancestry_assignment_method"]
        for name, factor in ANALYSES:
            vcf = directory / f"{name}.vcf.gz"
            with gzip.open(vcf, "wt", encoding="utf-8") as out:
                out.write(VCF_HEADER)
                for r in rows:
                    z = float(r["z"]) * factor
                    out.write(f"{r['chrom']}\t{r['pos']}\t.\t{r['ref']}\t{r['alt']}\t.\tPASS\t.\t"
                              f"EZ:ES:SE:AF\t{z}:{z}:1:0.3\n")
            lines.append(f"{name}\t{vcf}\t{name}\t1000\tsd\tdeclared_standardised\t\tEUR\tsource_fallback")
        (directory / "analyses.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        loaded = bundle.load(STORE_ID, registry_root=cls.registry)
        work = paths.work_dir(STORE_ID, root=cls.root)
        work.mkdir(parents=True)
        manifest.materialise_build_manifest(
            loaded.analyses_path,
            paths.build_manifest_path(STORE_ID, root=cls.root),
            work / "analyses.exclusions.json",
        )
        return loaded

    def test_the_plan_names_rho_on_the_hybrid_store_after_build(self) -> None:
        self.assertEqual([s.name for s in self.steps], ["build", "rho", "validate"])
        self.assertEqual(self.steps[1].argv[1], "build-dense-rho")

    def test_the_fixture_has_both_components(self) -> None:
        """Rho is built over a Hybrid that holds a Dense Component and Ragged Overflow (CONTRIBUTING)."""
        build = json.loads(run.load_record(STORE_ID, "build", root=self.root)["stdout"])
        self.assertGreater(build["n_panel"], 0)
        self.assertGreater(build["n_off_panel"], 0)

    def test_register_records_has_rho(self) -> None:
        self.assertIs(self.observed["has_rho"], True)
        written = yaml.safe_load((self.registry / STORE_ID / "validation.yaml").read_text(encoding="utf-8"))
        self.assertIs(written["observed"]["has_rho"], True)


class HybridCompletionRhoTest(unittest.TestCase):
    """A Hybrid that completes its own Store: complete -> completion-rho -> completion-validate -> register (#529).

    The workflow suite's tiny signal release (twelve variants, one Analysis), as its Hybrid
    completion tests build it, with `post.rho` declared, run once through the entry point.
    """

    store_id = "OGS-99530"

    @classmethod
    def setUpClass(cls) -> None:
        cls.td = Path(tempfile.mkdtemp(prefix="hybrid-completion-rho-"))
        cls.addClassCleanup(shutil.rmtree, cls.td, ignore_errors=True)
        cls.stores, cls.artifacts = prepare_hybrid_single_id_signal_release(cls.td, cls.store_id)
        build_yaml_p = cls.stores / cls.store_id / "build.yaml"
        recipe = yaml.safe_load(build_yaml_p.read_text(encoding="utf-8"))
        recipe["post"]["rho"] = True
        build_yaml_p.write_text(yaml.safe_dump(recipe), encoding="utf-8")
        cls.bundle = bundle.load(cls.store_id, registry_root=cls.stores)
        cls.result = subprocess.run(
            release_argv([cls.store_id], registry_root=cls.stores, artifact_root=cls.artifacts),
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )

    def test_the_own_id_plan_names_build_dense_rho_in_both_phases(self) -> None:
        steps = plan(self.bundle, artifact_root=self.artifacts)
        store = str(paths.store_path(self.store_id, root=self.artifacts))
        for name in ("rho", "completion-rho"):
            (step,) = [s for s in steps if s.name == name]
            self.assertEqual(step.argv[:3], ["opengwasdb", "build-dense-rho", step.argv[2]], name)
            self.assertEqual(len(step.argv), 3, name)
            self.assertEqual(Path(step.argv[2]).parent, Path(store).parent, name)
        names = [s.name for s in steps]
        self.assertLess(names.index("completion-rho"), names.index("completion-validate"))

    def test_the_run_registers_the_completed_store_with_has_rho(self) -> None:
        self.assertEqual(self.result.returncode, 0, self.result.stderr[-3000:])
        for name in ("rho", "completion-rho", "completion-validate"):
            record = run.load_record(self.store_id, name, root=self.artifacts)
            self.assertEqual(record["exit_code"], 0, name)
        written = yaml.safe_load((self.stores / self.store_id / "validation.yaml").read_text(encoding="utf-8"))
        self.assertIs(written["observed"]["has_rho"], True)


if __name__ == "__main__":
    unittest.main()
