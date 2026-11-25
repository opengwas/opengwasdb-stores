#!/usr/bin/env python3
"""A 0.3.0 Store through build -> rho -> clump -> validate -> register, with the real engine (#526).

The one smoke test of `post.clumps` against the engine; what `clump` computes is the
engine's own to test. The fixture is the engine's Test Panel (`fixtures/README.md`):

- its LD Store, built here with `opengwasdb ld build` (about 2 s), under the name the
  recipe's `post.ld_stores` maps EUR to;
- two GWAS-VCF Analyses over its 911 SNV rows (`a` at the panel's z, `b` at 0.9 z), both
  Assigned Ancestry EUR, in the shared Dense fixture bundle (`test_workflow.create_dense_fixture_store`)
  with the build manifest `manifest.materialise_build_manifest` derives from it.

`plan()` names `clump --ld-store EUR=...` after `rho` and `validate --ld-store EUR=...`
(a Dense build writes the Top-Hit Index inline, so no `top-hits` step). Each step runs as
`ogstores.run` runs it, then `register` writes `validation.yaml`, which must carry
`has_rho`, `has_clumps` and the engine's own `n_loci_5e8`/`n_unclumped_5e8`.

`clumps.reclump` is proven to run by a negative control. The engine reports findings only
for errors, warnings and skips, so a re-clump that agrees leaves no trace: the Clumps of a
copy of the published Store are corrupted and the planned `validate` argv, with its
`--ld-store`, must then report a `clumps.reclump` error.

Run from the repository root:
    pixi run -e dev python3 tests/clumps-pipeline/test_clumps_pipeline.py
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

from test_workflow import create_dense_fixture_store  # noqa: E402

from ogstores import bundle, manifest, paths, run  # noqa: E402
from ogstores.plan import plan  # noqa: E402
from ogstores.register import register_release  # noqa: E402

FIXTURES = HERE / "fixtures"
STORE_ID = "OGS-99526"
ANALYSES = (("a", 1.0), ("b", 0.9))
VCF_HEADER = (
    "##fileformat=VCFv4.2\n"
    '##FORMAT=<ID=EZ,Number=A,Type=Float,Description="Z-score">\n'
    '##FORMAT=<ID=ES,Number=A,Type=Float,Description="Effect size">\n'
    '##FORMAT=<ID=SE,Number=A,Type=Float,Description="Standard error">\n'
    '##FORMAT=<ID=AF,Number=A,Type=Float,Description="Allele frequency">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRAIT\n"
)
CLUMPS_LEAD = "data.zarr/top_hits/p_5e_08/clumps/default/lead"


def engine(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["opengwasdb", *argv], capture_output=True, text=True, check=False)


class ClumpsPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.td = Path(tempfile.mkdtemp(prefix="clumps-pipeline-"))
        cls.addClassCleanup(shutil.rmtree, cls.td, ignore_errors=True)
        panel = FIXTURES / "test-panel"
        cls.ld_eur = cls.td / "ld-stores" / "hgdp1kgp-v2-EUR"
        built = engine(
            "ld", "build", str(cls.ld_eur), "--pgen", str(panel / "panel.pgen"),
            "--keep", str(panel / "panel.keep"), "--pcs", str(panel / "pcs.eigenvec"),
            "--pc-source", "Test Panel", "--panel-id", "test-panel", "--ancestry", "EUR",
            "--genotype-source", "Test Panel", "--r-scale", "511", "--t-partner", "0.01",
            "--k", "10", "--info-min", "none",
        )
        assert built.returncode == 0, built.stderr
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
            cls.registry, STORE_ID, artifact_root=cls.root, post_overview=False, trial=True,
            post_extra={"rho": True, "clumps": True, "ld_stores": {"EUR": str(cls.ld_eur)}},
        )
        (directory / "test.vcf.gz").unlink()
        with open(FIXTURES / "rows.tsv", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
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

    def test_the_plan_names_clump_after_rho_and_validate_with_the_ld_store(self) -> None:
        # A Dense build writes the Top-Hit Index inline, so it plans no `top-hits` step.
        self.assertEqual([s.name for s in self.steps], ["build", "rho", "clump", "validate"])
        pair = f"EUR={self.ld_eur}"
        self.assertEqual(self.steps[2].argv[-2:], ["--ld-store", pair])
        self.assertEqual(self.steps[3].argv[-2:], ["--ld-store", pair])

    def test_register_records_has_rho_has_clumps_and_the_engines_counts(self) -> None:
        self.assertEqual(self.observed["format_version"], "0.3.0")
        self.assertIs(self.observed["has_rho"], True)
        self.assertIs(self.observed["has_clumps"], True)
        # The engine's own counts for this fixture: 16 loci in the 5e-8 tier, none unclumped.
        self.assertEqual((self.observed["n_loci_5e8"], self.observed["n_unclumped_5e8"]), (16, 0))
        written = yaml.safe_load((self.registry / STORE_ID / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(
            (written["observed"]["has_clumps"], written["observed"]["n_loci_5e8"]), (True, 16)
        )

    def test_clumps_reclump_runs_and_reports_a_corrupted_clump(self) -> None:
        """The planned validate argv, with its `--ld-store`, reports `clumps.reclump` for corrupted Clumps."""
        validate = run.load_record(STORE_ID, "validate", root=self.root)
        clean = {f["rule"] for f in json.loads(validate["stdout"])["findings"]}
        self.assertNotIn("clumps.reclump", clean)

        import numpy as np
        import zarr

        copy = Path(shutil.copytree(paths.store_path(STORE_ID, root=self.root), self.td / "corrupted"))
        lead = zarr.open_array(str(copy / CLUMPS_LEAD), mode="r+")
        lead[:] = np.asarray(lead[:])[::-1]
        argv = list(self.steps[3].argv)
        argv[2] = str(copy)
        result = engine(*argv[1:])
        findings = json.loads(result.stdout)["findings"]
        errors = [f for f in findings if f["rule"] == "clumps.reclump" and f["severity"] == "error"]
        self.assertTrue(errors, findings)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
