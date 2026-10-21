#!/usr/bin/env python3
"""Fixture-driven tests for issues #35-40's in-repository implementation."""
from __future__ import annotations

import csv, importlib.util, json, subprocess, sys, tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/ld-panel/construct_block.py"
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


def test_maf_floor() -> None:
    # #241's AMR pilot: 345 diploids, MAF >= 0.01 over called samples. MAC 7
    # (7/690) passes and MAC 6 does not, so an inherited MAC >= 50 must not apply.
    n = 345
    rng = np.random.default_rng(241)
    def hets(k: int) -> np.ndarray: return np.r_[np.ones(k), np.zeros(n - k)]
    sparse = hets(6); sparse[-45:] = np.nan  # MAF 6/600 = 0.01 on the 300 called
    g = np.column_stack([
        hets(6), hets(7), sparse, np.full(n, np.nan), np.zeros(n), 2 - hets(7),
        *[rng.binomial(2, .3, n).astype(float) for _ in range(20)],
    ])
    variants = [{"chrom": "1", "pos": 100 + i, "allele1": "C", "allele2": "G"}
                for i in range(g.shape[1])]
    result = module.construct_block(g, variants, maf_threshold=.01,
        cumulative_variance_target=.99, component_floor=5)
    p = result.provenance
    check([v["BP"] for v in result.variant_table] == [101, 102, 105, *range(106, 126)],
          "MAF floor kept the wrong variants")
    check(p["maf_threshold"] == .01 and p["mac_threshold"] is None,
          "a MAF floor must replace, not add to, the MAC floor")
    check((p["n_variants_all_missing"], p["n_variants_invariant"], p["n_variants_below_floor"]) == (1, 1, 1),
          "dropped-variant classes are miscounted")
    check(p["below_floor_mac_counts"] == {"6": 1}, "below-floor MAC strata are wrong")
    check(np.isclose(p["missing_call_rate"], (45 + n) / (n * g.shape[1])), "input missingness is wrong")
    check(np.isclose(p["max_missing_call_rate_retained"], 45 / n), "retained missingness is wrong")
    mac_only = module.construct_block(g, variants, mac_threshold=7, component_floor=5).provenance
    check(mac_only["mac_threshold"] == 7 and mac_only["maf_threshold"] is None and
          mac_only["n_variants_retained"] == 22 and mac_only["below_floor_mac_counts"] == {"6": 2},
          "the MAC floor changed")
    for script in ("construct_block.py", "run_panels.py"):
        both = subprocess.run([sys.executable, str(SCRIPT.with_name(script)), "--maf", ".01", "--mac", "50"],
                              capture_output=True, text=True)
        check(both.returncode == 2 and "not allowed with" in both.stderr,
              f"{script} accepted a MAC floor alongside a MAF floor")
    # Pilot conversion takes every biallelic site: the floor belongs to construct_block.
    command = materialize.conversion_command(Path("plink2"), Path("block.vcf.gz"),
                                             Path("AMR.keep"), Path("AMR/chr22"), None)
    check("--mac" not in command and "--maf" not in command and "--max-alleles" in command,
          "pilot PGEN conversion applied a frequency floor")
    region = materialize.region_vcf_command(Path("bcftools"), Path("chr22.vcf.bgz"),
                                            "chr22:100-200", Path("AMR.ids"), Path("block.vcf.gz"))
    check(region[:2] == ["bcftools", "view"] and
          region[region.index("--regions") + 1] == "chr22:100-200" and
          region[region.index("--samples-file") + 1] == "AMR.ids" and
          "--force-samples" not in region and region[region.index("--output-type") + 1] == "z",
          "region extraction must subset by index and fail on absent samples")


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


AMR_PILOT = ROOT / "resources/reference-resources/hgdp1kgp-hg38-ld/amr-pilot"


def test_amr_pilot_mapping() -> None:
    def table(path: Path) -> list[dict[str, str]]: return list(csv.DictReader(path.open(), delimiter="\t"))
    production = {r["population"] for r in table(AMR_PILOT.parent / "populations_to_superpop.tsv")}
    pilot = {r["population"]: r for r in table(AMR_PILOT / "populations_to_superpop.tsv")}
    included = {p for p, r in pilot.items() if r["status"] == "included"}
    check(set(pilot) == production, "the pilot mapping must label exactly the production populations")
    check(included == {"CLM", "MXL", "PEL", "PUR"} and all(pilot[p]["panel"] == "AMR" for p in included),
          "the pilot includes only the four 1kGP AMR populations")
    check(all(pilot[p]["reason"] == "hgdp_excluded_amr_pilot"
              for p in ("Pima", "Maya", "Karitiana", "Surui", "Colombian")),
          "HGDP American populations must be excluded by name")
    check(all(r["status"] == "excluded" and not r["panel"] and r["reason"]
              for p, r in pilot.items() if p not in included), "population is neither mapped nor excluded")
    samples = table(AMR_PILOT / "phase3-amr-samples.tsv")
    pops = {p: sum(r["pop"] == p for r in samples) for p in included}
    check(len({r["sample"] for r in samples}) == len(samples) == 347 and
          pops == {"CLM": 94, "MXL": 64, "PEL": 85, "PUR": 104} and
          all(r["super_pop"] == "AMR" for r in samples), "the Phase 3 AMR sample list changed")


def test_amr_pilot_membership() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp); cohort = tmp / "cohort.tsv"; mapping = tmp / "mapping.tsv"
        cohort.write_text(
            "sample\tpopulation\tproject\tgenetic_region\thigh_quality\n"
            "P1\tCLM\t1000 Genomes\tAMR\ttrue\n"   # Phase 3, high quality: the only member
            "P2\tCLM\t1000 Genomes\tAMR\tfalse\n"  # Phase 3, fails high_quality
            "X1\tCLM\t1000 Genomes\tAMR\ttrue\n"   # 30x expansion, not Phase 3
            "H1\tMaya\tHGDP\tAMR\ttrue\n"
            "E1\tGBR\t1000 Genomes\tEUR\ttrue\n")
        header = "population\tpanel\tstatus\treason\tsource_regions\n"
        mapping.write_text(header + "CLM\tAMR\tincluded\tamr_pilot_1kgp\tAMR\n"
                           "Maya\t\texcluded\thgdp_excluded_amr_pilot\tAMR\n"
                           "GBR\t\texcluded\toutside_amr_pilot\tEUR\n")
        def run(samples: str, mapping: Path = mapping) -> subprocess.CompletedProcess[str]:
            (tmp / "samples.tsv").write_text("sample\tpop\n" + samples)
            return subprocess.run([sys.executable, str(SCRIPT.with_name("make_membership.py")),
                "--cohort", str(cohort), "--mapping", str(mapping), "--panels", "AMR",
                "--samples", str(tmp / "samples.tsv"), "--out", str(tmp / "out")],
                capture_output=True, text=True)
        result = run("P1\tCLM\nP2\tCLM\nH1\tMaya\n")
        check(result.returncode == 0 and (tmp / "out/AMR.keep").read_text() == "#IID\nP1\n" and
              not (tmp / "out/AFR.keep").exists() and
              json.loads((tmp / "out/membership.json").read_text()) == {"AMR": 1},
              "AMR membership admitted a non-Phase-3, low-quality or HGDP sample")
        check(run("P1\tCLM\nZ9\tCLM\n").returncode != 0, "an allowlisted sample absent from the cohort must fail")
        check(run("P1\tPUR\n").returncode != 0, "an allowlist population disagreeing with the cohort must fail")
        eur = tmp / "eur.tsv"
        eur.write_text(header + "CLM\tAMR\tincluded\tamr_pilot_1kgp\tAMR\nMaya\t\texcluded\tx\tAMR\n"
                       "GBR\tEUR\tincluded\tcontinental_panel\tEUR\n")
        check(run("P1\tCLM\n", eur).returncode != 0, "a mapping panel outside --panels must fail")


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
    test_block(); test_maf_floor(); test_canonical_effect_allele_ld_orientation()
    test_mapping(); test_membership_keep_format(); test_amr_pilot_mapping(); test_amr_pilot_membership()
    test_index_validation(); test_compressed_pvar_input()
    test_plink_raw_input()
    for script in ("acquire_hgdp1kgp.py", "construct_block.py", "run_panels.py"):
        subprocess.run([sys.executable, str(SCRIPT.with_name(script)), "--help"], check=True,
                       stdout=subprocess.DEVNULL)
    print(f"ok — {checks} checks passed")

if __name__ == "__main__": main()
