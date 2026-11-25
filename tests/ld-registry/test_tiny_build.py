#!/usr/bin/env python3
"""One tiny real build through the recipe, then a rebuild from its record (#541).

A synthetic two-chromosome panel (60 samples, 120 variants a chromosome,
written with pgenlib, the engine's own reader's library) is built as a
Genotype Store by `ld_registry.recipe` and the real `opengwasdb ld build` and
`validate`, and its record written with the build's `rebuild`. That record is
then rebuilt into a new directory, and the two Stores' `opengwasdb ld info`
must differ only in the build's own fields. No host data is read; each engine
command costs about two seconds, nearly all of it the CLI starting. The one
thing not run for real is the published LD Store whose `source.sha256` the
staged copies are checked against: no LD Store of this panel exists, so the
test answers that one `opengwasdb ld info` with checksums it computed itself.

Run from the repository root:
    python3 tests/ld-registry/test_tiny_build.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import yaml

from ld_registry import compare, rebuild, recipe, record

PANEL = "T"
CHROMS = (21, 22)
N_SAMPLES = 60
N_VARIANTS = 120


def write_panel(root: Path) -> recipe.Inputs:
    """A panel in the recipe's layout: `pgen/T/chr{21,22}.*`, `keep/T.keep` and `pcs/T.eigenvec`."""
    import pgenlib

    rng = np.random.default_rng(541)
    ids = [f"S{i:03d}" for i in range(N_SAMPLES)]
    (root / "keep").mkdir(parents=True)
    (root / "keep" / f"{PANEL}.keep").write_text("#IID\n" + "".join(f"{s}\n" for s in ids))
    pcs = rng.normal(size=(N_SAMPLES, 12))
    (root / "pcs").mkdir()
    (root / "pcs" / f"{PANEL}.eigenvec").write_text(
        "#IID\t" + "\t".join(f"PC{i + 1}" for i in range(12)) + "\n"
        + "".join(s + "\t" + "\t".join(f"{v:.6f}" for v in row) + "\n" for s, row in zip(ids, pcs))
    )
    for chrom in CHROMS:
        prefix = root / "pgen" / PANEL / f"chr{chrom}"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        Path(f"{prefix}.psam").write_text("#IID\tSEX\n" + "".join(f"{s}\tNA\n" for s in ids))
        positions = np.sort(rng.choice(np.arange(10_000_000, 12_000_000), N_VARIANTS, replace=False))
        lines = ["#CHROM\tPOS\tID\tREF\tALT\tFILTER"]
        for position in positions:
            ref, alt = "ACGT"[position % 4], "ACGT"[(position + 1) % 4]
            lines.append(f"{chrom}\t{position}\t{chrom}:{position}:{ref}:{alt}\t{ref}\t{alt}\tPASS")
        Path(f"{prefix}.pvar").write_text("\n".join(lines) + "\n")
        writer = pgenlib.PgenWriter(f"{prefix}.pgen".encode(), N_SAMPLES, N_VARIANTS, False, hardcall_phase_present=True)
        haplotypes = rng.integers(0, 2, size=2 * N_SAMPLES, dtype=np.int32)
        for _ in range(N_VARIANTS):  # LD: each variant flips some of its neighbour's alleles
            haplotypes = np.where(rng.random(2 * N_SAMPLES) < 0.15, 1 - haplotypes, haplotypes).astype(np.int32)
            writer.append_alleles(haplotypes, all_phased=True)
        writer.close()
    return recipe.Inputs(root / "pgen", root / "keep", root / "pcs", chroms=CHROMS, ld_stores_root=root / "stores")


def answering_the_ld_store(inputs: recipe.Inputs):
    """`opengwasdb ld info` for real, except of the panel's LD Store, which holds the inputs' sha256."""
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    files = [inputs.pgen_root / PANEL / f"chr{c}.{e}" for c in CHROMS for e in ("pgen", "psam", "pvar")]
    files += [inputs.keep_root / f"{PANEL}.keep"]
    # OGL-00003's shape: the PC File's digest is `adjustment.pc_file.sha256`, not in `source.sha256`.
    payload = {
        "source": {"kind": "genotypes", "sha256": {path.name: digest(path) for path in files}},
        "adjustment": {"pc_file": {"sha256": digest(inputs.pcs_root / f"{PANEL}.eigenvec"), "source": "tests"}},
    }
    ld_store = str(inputs.ld_stores_root / recipe.store_name(PANEL))

    def run(argv):
        if argv[-1] == ld_store:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        return subprocess.run(list(argv), capture_output=True, text=True, check=False)

    return run


@unittest.skipUnless(shutil.which("opengwasdb"), "the engine's command line is not installed")
class TestATinyRealBuild(unittest.TestCase):
    def test_the_recipe_builds_and_a_rebuild_from_its_record_is_the_same_store(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            ld_root = tmp / "stores" / "ld"
            inputs = write_panel(tmp / "in")
            plan = recipe.plan_build("genotype_store", PANEL, tmp / "built", inputs, threads=2)
            written = recipe.build_store(
                plan, ld_root, "OGL-00099", version="test", environment="dev", info_runner=answering_the_ld_store(inputs)
            )

            document = yaml.safe_load(written.path.read_text(encoding="utf-8"))
            self.assertEqual(document["kind"], "genotype_store")
            self.assertEqual(document["provenance"]["n"], N_SAMPLES)
            self.assertEqual(document["rebuild"]["script"], "opengwasdb ld build --genotypes")
            missing = {error.field for error in record.load_record(written.path)[1]}
            self.assertEqual(missing, {"label", "status", "licence", "used_for"})

            before = written.path.read_bytes()
            again = rebuild.rebuild_store(ld_root, "OGL-00099", tmp / "rebuilt")
            found, problems = compare.accept(
                again.original, again.store, recorded_sha256=again.recorded_sha256, keep=again.keep,
            )
            self.assertEqual(again.original, plan.store)
            self.assertEqual(again.commands[-1][2], "validate", "the recorded validate ran, once")
            self.assertEqual(again.keep, tmp / "rebuilt" / "inputs" / f"{PANEL}.keep")
            self.assertEqual(problems, [])
            self.assertEqual(compare.failures(found), [], compare.render(found))
            self.assertIn("build.created_at", [d.field for d in found])
            self.assertEqual(written.path.read_bytes(), before, "a rebuild never writes the record")


if __name__ == "__main__":
    unittest.main()
