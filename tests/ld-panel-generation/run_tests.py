#!/usr/bin/env python3
"""Fixture-driven tests for issues #35-40's in-repository implementation."""
from __future__ import annotations

import csv, importlib.util, json, subprocess, sys, tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "resources/scripts/ld-panel/construct_block.py"
spec = importlib.util.spec_from_file_location("construct_block", SCRIPT)
module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
assert spec.loader; spec.loader.exec_module(module)

acquire_spec = importlib.util.spec_from_file_location("acquire_hgdp1kgp", SCRIPT.with_name("acquire_hgdp1kgp.py"))
acquire = importlib.util.module_from_spec(acquire_spec); sys.modules[acquire_spec.name] = acquire
assert acquire_spec.loader; acquire_spec.loader.exec_module(acquire)
materialize_spec = importlib.util.spec_from_file_location("materialize_reference", SCRIPT.with_name("materialize_reference.py"))
materialize = importlib.util.module_from_spec(materialize_spec); sys.modules[materialize_spec.name] = materialize
assert materialize_spec.loader; materialize_spec.loader.exec_module(materialize)

checks = 0
def check(value: bool, message: str) -> None:
    global checks; checks += 1
    if not value: raise AssertionError(message)


def test_block() -> None:
    rng = np.random.default_rng(35)
    n = 40
    latent = rng.integers(0, 3, n)
    # MACs are 20 (excluded), 21 (included), while correlated columns make
    # reconstruction and rank deficiency directly observable.
    g = np.column_stack([
        np.r_[np.ones(20), np.zeros(20)],
        np.r_[np.ones(21), np.zeros(19)],
        latent, latent, 2 - latent,
        *[rng.integers(0, 3, n) for _ in range(45)],
    ])
    variants = [{"chrom": "chr1", "pos": 100 + i, "allele1": "T", "allele2": "A"}
                for i in range(g.shape[1])]
    result = module.construct_block(g, variants, mac_threshold=21,
        cumulative_variance_target=.9, component_floor=2, interval="1:100-200")
    check(result.provenance["n_variants_retained"] == 49, "MAC boundary filtering failed")
    check(result.eigenvectors.shape[0] > n, "fixture is not rank deficient")
    check(len(result.eigenvalues) <= n,
          "rank-deficient blocks must not materialize the full variant-by-variant spectrum")
    reconstructed = (result.eigenvectors * result.eigenvalues[:result.eigenvectors.shape[1]]) @ result.eigenvectors.T
    # Retained columns 1:4 are perfectly +/- correlated (column 0 is the MAC
    # boundary variant); a 90%-variance truncation must preserve that block.
    check(np.allclose(np.abs(reconstructed[1:4, 1:4]), 1, atol=.2), "known correlation not reconstructed")
    check(result.provenance["achieved_variance"] >= .9, "variance target not reached")
    check(result.provenance["components_retained"] == result.eigenvectors.shape[1], "provenance disagrees")
    check(all(v["SNP"] == f"1:{v['BP']}:A:T" and v["EA"] == "A" for v in result.variant_table),
          "ALIDs are not canonical")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "1" / "100-200"
        module._atomic_outputs(base, result)
        check(base.with_suffix(".provenance.json").exists(), "atomic completion marker missing")
        p = json.loads(base.with_suffix(".provenance.json").read_text())
        d = np.load(base.with_suffix(".ldeig.npz"))
        check(p["components_retained"] == d["vectors"].shape[1], "written provenance mismatch")


def test_canonical_effect_allele_ld_orientation() -> None:
    dosage = np.tile([0.0, 1.0, 2.0, 1.0], 10)
    # PLINK exports ALT dosage. Variant 1's canonical effect allele is ALT,
    # while variant 2's is REF, so their canonical-EA LD must be -1.
    genotypes = np.column_stack([dosage, dosage])
    variants = [
        {"chrom": "1", "pos": 100, "allele1": "T", "allele2": "A"},
        {"chrom": "1", "pos": 200, "allele1": "A", "allele2": "G"},
    ]
    result = module.construct_block(genotypes, variants, mac_threshold=1,
                                    cumulative_variance_target=1, component_floor=2)
    reconstructed = (result.eigenvectors * result.eigenvalues) @ result.eigenvectors.T
    check(np.isclose(reconstructed[0, 1], -1.0),
          "eigenvectors are not oriented to the canonical effect allele")


def test_mapping() -> None:
    path = ROOT / "resources/reference-resources/hgdp1kgp-hg38-ld/populations_to_superpop.tsv"
    rows = list(csv.DictReader(path.open(), delimiter="\t")); by_pop = {r["population"]: r for r in rows}
    check(len(rows) == len(by_pop) == 79, "population labels are missing or duplicated")
    check(all((r["status"] == "included" and r["panel"] in {"AFR","EAS","SAS","EUR"}) or
              (r["status"] == "excluded" and not r["panel"] and r["reason"]) for r in rows),
          "population is neither mapped nor explicitly excluded")
    check(all(by_pop[p]["status"] == "excluded" for p in ("ASW", "ACB")), "admixed populations included")
    check(not any(r["panel"] in {"AMR","MID","NAF"} for r in rows), "panel-less superpopulation mapped")


def test_membership_keep_format() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp); cohort = tmp / "cohort.tsv"; mapping = tmp / "mapping.tsv"; out = tmp / "out"
        cohort.write_text("sample\tpopulation\tproject\tgenetic_region\thigh_quality\nS1\tPOP\tHGDP\tAFR\ttrue\n")
        mapping.write_text("population\tpanel\tstatus\treason\tsource_regions\nPOP\tAFR\tincluded\tcontinental_panel\tAFR\n")
        subprocess.run([sys.executable, str(SCRIPT.with_name("make_membership.py")),
                        "--cohort", str(cohort), "--mapping", str(mapping), "--out", str(out)],
                       check=True, capture_output=True, text=True)
        check((out / "AFR.keep").read_text() == "#IID\nS1\n",
              "PLINK keep file must be an explicit one-column IID list")


def test_index_validation() -> None:
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        # The upstream index has no optional count metadata, so `index --stats`
        # fails; an indexed region query succeeds and is the required check.
        if cmd[1:3] == ["index", "--stats"]: return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)
    acquire.verify_index(Path("gnomad.genomes.v3.1.2.hgdp_tgp.chr1.vcf.bgz"), run=run)
    check(calls and calls[0][1] == "view" and "--regions" in calls[0] and
          calls[0][calls[0].index("--output-type") + 1] == "v",
          "index validation did not exercise an indexed region query")
    command = materialize.conversion_command(Path("plink2"), Path("chr1.vcf.bgz"),
                                             Path("AFR.keep"), Path("AFR/chr1"), 50)
    check(command[command.index("--mac") + 1] == "50" and
          command[command.index("--keep") + 1] == "AFR.keep" and "--nonfounders" in command and
          command[command.index("--threads") + 1] == "8" and
          command[command.index("--memory") + 1] == "32768" and
          "pvar-cols=xheader" in command and "--snps-only" not in command,
          "PGEN conversion does not apply ancestry-specific MAC filtering")


def test_compressed_pvar_input() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "chr22"
        Path(str(prefix) + ".pvar.zst").touch()
        check(module.plink_source_args(prefix, None) == ["--pfile", str(prefix), "vzs"],
              "compressed PVAR input must pass PLINK's vzs modifier")


def test_plink_raw_input() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "block.raw"
        raw.write_text(
            "#FID\tIID\tSID\tPAT\tMAT\tSEX\tvar_A\tindel_DEL\n"
            "0\tS1\t0\t0\t0\tNA\t0\t1\n"
            "0\tS2\t0\t0\t0\t2\tNA\t2\n"
        )
        genotypes, names, ids = module.read_plink_raw(raw)
        check(names == ["var_A", "indel_DEL"], "PLINK raw variant headers were changed")
        check(ids == ["S1", "S2"], "PLINK raw sample IDs were parsed incorrectly")
        check(genotypes.shape == (2, 2) and np.isnan(genotypes[1, 0]) and
              np.allclose(genotypes[0], [0, 1]),
              "PLINK raw dosages or missing values were parsed incorrectly")


def main() -> None:
    test_block(); test_canonical_effect_allele_ld_orientation()
    test_mapping(); test_membership_keep_format(); test_index_validation(); test_compressed_pvar_input()
    test_plink_raw_input()
    for script in ("acquire_hgdp1kgp.py", "construct_block.py", "run_panels.py"):
        subprocess.run([sys.executable, str(SCRIPT.with_name(script)), "--help"], check=True,
                       stdout=subprocess.DEVNULL)
    print(f"ok — {checks} checks passed")

if __name__ == "__main__": main()
