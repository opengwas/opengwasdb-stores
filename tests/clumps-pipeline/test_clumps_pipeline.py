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

`AddedInPlaceTest` is the one real-engine run of adding post steps to a published Store
in place (#528, ADR 0044), sharing this module's LD Store: the same fixture, published
with `overview` and no Rho Matrix or Clumps, gains them through `pixi run release <id>`,
and a second run then does nothing. `tests/post-additions` tests the mode without the engine.

Run from the repository root:
    pixi run -e dev python3 tests/clumps-pipeline/test_clumps_pipeline.py
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "workflow"))

from test_workflow import create_dense_fixture_store  # noqa: E402

from ogstores import bundle, manifest, paths, post_additions, release, run  # noqa: E402
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


def build_ld_store(destination: Path) -> Path:
    """The Test Panel's EUR LD Store at `destination`, with `opengwasdb ld build` (about 2 s)."""
    panel = FIXTURES / "test-panel"
    built = engine(
        "ld", "build", str(destination), "--pgen", str(panel / "panel.pgen"),
        "--keep", str(panel / "panel.keep"), "--pcs", str(panel / "pcs.eigenvec"),
        "--pc-source", "Test Panel", "--panel-id", "test-panel", "--ancestry", "EUR",
        "--genotype-source", "Test Panel", "--r-scale", "511", "--t-partner", "0.01",
        "--k", "10", "--info-min", "none",
    )
    assert built.returncode == 0, built.stderr
    return destination


def fixture_bundle(
    registry: Path,
    root: Path,
    store_id: str = STORE_ID,
    *,
    post_extra: dict | None = None,
    post_overview: bool = False,
) -> bundle.Bundle:
    """The Dense fixture bundle over the Test Panel's SNVs, with its derived build manifest.

    Two GWAS-VCF Analyses, `a` at the panel's z and `b` at 0.9 z, both Assigned
    Ancestry EUR. `post_extra` adds keys to the recipe's `post` block. Also used
    by `tests/post-additions` (#528).
    """
    directory = create_dense_fixture_store(
        registry, store_id, artifact_root=root, post_overview=post_overview, trial=True, post_extra=post_extra,
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
    loaded = bundle.load(store_id, registry_root=registry)
    work = paths.work_dir(store_id, root=root)
    work.mkdir(parents=True)
    manifest.materialise_build_manifest(
        loaded.analyses_path,
        paths.build_manifest_path(store_id, root=root),
        work / "analyses.exclusions.json",
    )
    return loaded


TD: Path
LD_EUR: Path


def setUpModule() -> None:
    """The LD Store both classes clump against, built once."""
    global TD, LD_EUR
    TD = Path(tempfile.mkdtemp(prefix="clumps-pipeline-"))
    unittest.addModuleCleanup(shutil.rmtree, TD, ignore_errors=True)
    LD_EUR = build_ld_store(TD / "ld-stores" / "hgdp1kgp-v2-EUR")


class ClumpsPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.td = Path(tempfile.mkdtemp(dir=TD))
        cls.ld_eur = LD_EUR
        cls.registry, cls.root = cls.td / "stores", cls.td / "artifacts"
        cls.bundle = fixture_bundle(
            cls.registry, cls.root,
            post_extra={"rho": True, "clumps": True, "ld_stores": {"EUR": str(cls.ld_eur)}},
        )
        cls.steps = plan(cls.bundle, artifact_root=cls.root)
        for step in cls.steps:
            run.execute_step(step, bundle=cls.bundle, artifact_root=cls.root)
        cls.observed = register_release(
            cls.bundle, registry_root=cls.registry, artifact_root=cls.root
        )["observed"]

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



class AddedInPlaceTest(unittest.TestCase):
    """A Snakemake-built 0.3.0 Store gains Rho and Clumps in place, with the real engine (#528, ADR 0044).

    The Store is built and published by `pixi run release`, so Snakemake holds
    metadata for every record. The addition is interrupted part-way through
    clearing that metadata, after its marker is written; the next run finishes
    the publication and the cleanup, and then `release all` and a direct
    Snakemake dry run find nothing to do.
    """

    STORE = "OGS-99528"

    def setUp(self) -> None:
        td = Path(tempfile.mkdtemp(dir=TD))
        self.td, self.registry, self.root = td, td / "stores", td / "artifacts"
        fixture_bundle(self.registry, self.root, self.STORE, post_overview=True)
        (td / "snakemake").mkdir()
        os.environ[release.SNAKEMAKE_DIRECTORY_ENV] = str(td / "snakemake")
        self.addCleanup(os.environ.pop, release.SNAKEMAKE_DIRECTORY_ENV, None)
        code, out = self.release()
        assert code == 0, out
        build_p = self.registry / self.STORE / "build.yaml"
        recipe = yaml.safe_load(build_p.read_text(encoding="utf-8"))
        recipe["post"].update({"rho": True, "clumps": True, "ld_stores": {"EUR": str(LD_EUR)}})
        build_p.write_text(yaml.safe_dump(recipe), encoding="utf-8")

    def release(self, target: str | None = None) -> tuple[int, str]:
        out = io.StringIO()
        words = [target or self.STORE, "--cores", "1", "--config", f"registry_root={self.registry}", f"artifact_root={self.root}"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = release.main(words)
        return code, out.getvalue()

    def test_a_published_store_gains_rho_and_clumps_and_an_interrupted_cleanup_recovers(self) -> None:
        build_p = paths.record_path(self.STORE, "build", root=self.root)
        build_before = (build_p.stat().st_mtime_ns, build_p.read_bytes())
        overview_before = paths.record_path(self.STORE, "overview", root=self.root).stat().st_mtime_ns
        real_cleanup = release._cleanup_metadata

        def one_deletion(inv: release.Invocation, files, *, root: Path) -> None:
            real_cleanup(inv, list(files)[:1], root=root)
            raise KeyboardInterrupt("signal 15")

        with mock.patch.object(release, "_cleanup_metadata", side_effect=one_deletion):
            code, out = self.release()

        self.assertEqual(code, 130, out)
        self.assertIn("had begun", out)
        code, out = self.release()
        self.assertEqual(code, 0, out)
        self.assertIn("completed the interrupted publication", out)

        written = yaml.safe_load((self.registry / self.STORE / "validation.yaml").read_text(encoding="utf-8"))
        observed = written["observed"]
        self.assertEqual((observed["format_version"], observed["has_rho"], observed["has_clumps"]), ("0.3.0", True, True))
        self.assertEqual((observed["n_loci_5e8"], observed["n_unclumped_5e8"]), (16, 0))
        self.assertEqual(written[post_additions.BLOCK][0]["steps"], ["rho", "clump", "overview"])
        self.assertTrue((Path(written["replaced"]["archive"]) / "store.opengwasdb").is_dir())
        # No rebuild; the declared overview is regenerated; validate re-clumps against the LD Store.
        self.assertEqual((build_p.stat().st_mtime_ns, build_p.read_bytes()), build_before)
        self.assertGreater(paths.record_path(self.STORE, "overview", root=self.root).stat().st_mtime_ns, overview_before)
        self.assertIn("--ld-store", run.load_record(self.STORE, "validate", root=self.root)["argv"])

        self.assertIn("nothing to be done", out)
        code, out = self.release("all")
        self.assertEqual((code, "nothing to be done" in out), (0, True), out)
        dry = subprocess.run(
            [sys.executable, "-m", "snakemake", "--snakefile", str(release.SNAKEFILE), "--dry-run", self.STORE,
             "--config", f"registry_root={self.registry}", f"artifact_root={self.root}"],
            cwd=self.td / "snakemake", capture_output=True, text=True,
        )
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("Nothing to be done", dry.stdout + dry.stderr)


if __name__ == "__main__":
    unittest.main()
