#!/usr/bin/env python3
"""Fixture-driven tests for issues #35-40's in-repository implementation."""
from __future__ import annotations

import csv, importlib.util, json, shutil, subprocess, sys, tempfile
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
verify_spec = importlib.util.spec_from_file_location("verify_block", SCRIPT.with_name("verify_block.py"))
verify = importlib.util.module_from_spec(verify_spec); sys.modules[verify_spec.name] = verify
assert verify_spec.loader; verify_spec.loader.exec_module(verify)
calibration_spec = importlib.util.spec_from_file_location("heldout_calibration", SCRIPT.with_name("heldout_calibration.py"))
calibration = importlib.util.module_from_spec(calibration_spec); sys.modules[calibration_spec.name] = calibration
assert calibration_spec.loader; calibration_spec.loader.exec_module(calibration)

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
    check(np.isclose(p["max_missing_call_rate_retained"], 45 / n) and
          p["retained_missing_call_rate_over"] == {"0.05": 1, "0.1": 1, "0.5": 0},
          "retained missingness is wrong")
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


def test_alid_collisions() -> None:
    # gnomAD splits multi-allelic sites, so the insertion G>GA and the deletion
    # GA>G at one position both canonicalise to ALID 1:100:G:GA (#241).
    rng = np.random.default_rng(7)
    g = rng.binomial(2, .3, (60, 4)).astype(float)
    variants = [{"chrom": "1", "pos": 100, "allele1": "G", "allele2": "GA"},
                {"chrom": "1", "pos": 100, "allele1": "GA", "allele2": "G"},
                {"chrom": "1", "pos": 100, "allele1": "G", "allele2": "A"},
                {"chrom": "1", "pos": 101, "allele1": "C", "allele2": "T"}]
    result = module.construct_block(g, variants, mac_threshold=1, component_floor=1)
    check([r["SNP"] for r in result.variant_table] == ["1:100:A:G", "1:101:C:T"] and
          result.provenance["n_variants_alid_collision"] == 2,
          "an ALID naming two source records must be excluded, both copies")
    g[:, 1] = 0  # the deletion is invariant here, yet the ALID still names two records
    again = module.construct_block(g, variants, mac_threshold=1, component_floor=1).provenance
    check((again["n_variants_retained"], again["n_variants_alid_collision"], again["n_variants_invariant"]) == (2, 1, 1),
          "a collision must be excluded even when its other record would be dropped anyway")


def ld_fixture(n: int = 120, seed: int = 241) -> tuple[np.ndarray, list[dict[str, object]]]:
    """ALT dosages in four LD groups; even columns have EA = REF, odd EA = ALT."""
    rng = np.random.default_rng(seed)
    latent = rng.binomial(2, .4, (n, 4)).astype(float)
    columns = []
    for j in range(40):
        column = latent[:, j % 4].copy(); noise = rng.random(n) < .1
        column[noise] = rng.binomial(2, .4, noise.sum()); columns.append(column)
    variants = [{"chrom": "22", "pos": 100 + j, "allele1": ("A", "T")[j % 2], "allele2": ("C", "G")[j % 2]}
                for j in range(40)]
    return np.column_stack(columns), variants


def test_verify_block() -> None:
    g, variants = ld_fixture()
    result = module.construct_block(g, variants, mac_threshold=1, component_floor=5)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "AMR" / "22" / "100-200"
        module._atomic_outputs(base, result)
        report = verify.verify_block(base, g, variants, thresh=.9)
        check(report["failures"] == [] and report["core_components_at_thresh"] == report["components_at_thresh"],
              f"a correct block failed verification: {report['failures']}")
        def failures(edit) -> list[str]:
            with tempfile.TemporaryDirectory() as tmp2:
                copy = Path(tmp2) / "AMR" / "22" / "100-200"; copy.parent.mkdir(parents=True)
                for suffix in (".tsv", ".ldeig.npz", ".provenance.json"):
                    shutil.copy(base.with_suffix(suffix), copy.with_suffix(suffix))
                arrays = dict(np.load(copy.with_suffix(".ldeig.npz")))
                provenance = json.loads(copy.with_suffix(".provenance.json").read_text())
                edit(copy, arrays, provenance)
                np.savez(copy.with_suffix(".ldeig.npz"), **arrays)
                copy.with_suffix(".provenance.json").write_text(json.dumps(provenance))
                return verify.verify_block(copy, g, variants, thresh=.9)["failures"]
        def swap_rows(base, arrays, provenance):
            lines = base.with_suffix(".tsv").read_text().splitlines(keepends=True)
            lines[1], lines[2] = lines[2], lines[1]; base.with_suffix(".tsv").write_text("".join(lines))
        def flip_row(base, arrays, provenance): arrays["vectors"][3] *= -1
        def nan(base, arrays, provenance): arrays["vectors"][0, 0] = np.nan
        def negative(base, arrays, provenance): arrays["values"][-1] = -1.0
        def under_resolved(base, arrays, provenance):
            arrays["vectors"] = arrays["vectors"][:, :1]; provenance["components_retained"] = 1
        check("binding" in failures(swap_rows), "permuted rows passed the binding check")
        check("binding" in failures(flip_row), "a flipped allele passed the binding check")
        check("finite" in failures(nan), "a non-finite eigenvector passed")
        check("non_negative" in failures(negative), "a negative eigenvalue passed")
        check(failures(under_resolved) == ["resolved_at_thresh"], "an under-resolved block passed")


def test_heldout_calibration() -> None:
    populations = ["CLM"] * 92 + ["MXL"] * 64 + ["PEL"] * 85 + ["PUR"] * 104
    test = calibration.split_individuals(populations, test_fraction=1 / 3, seed=1)
    by_pop = {p: int(sum(t for t, q in zip(test, populations) if q == p)) for p in set(populations)}
    check(by_pop == {"CLM": 31, "MXL": 21, "PEL": 28, "PUR": 35}, f"the held-out split is not stratified: {by_pop}")
    check(np.array_equal(test, calibration.split_individuals(populations, test_fraction=1 / 3, seed=1)) and
          not np.array_equal(test, calibration.split_individuals(populations, test_fraction=1 / 3, seed=2)),
          "the held-out split is not determined by its seed")
    g, variants = ld_fixture(n=300)
    result = calibration.calibrate_block(g, variants, ["POP"] * 300, maf_threshold=.01,
                                         replicates=5, component_floor=5, seed=3)
    check(result["n_train"] + result["n_test"] == 300 and result["n_test"] == 100,
          "train and test individuals must partition the cohort")
    by_panel = {p: [r for r in result["replicates"] if r["panel"] == p] for p in ("held_out", "test_in_sample")}
    check(all(len(rows) == 5 for rows in by_panel.values()), "each replicate is evaluated on both panels")
    check(all(r["accepted"] for rows in by_panel.values() for r in rows) and
          all(np.median([r["pearson_r"] for r in rows]) > .9 for rows in by_panel.values()),
          "strong-LD masked z-scores were not recovered")
    # Without LD a masked z-score is unpredictable, so the metric must say so.
    rng = np.random.default_rng(4)
    unlinked = rng.binomial(2, .4, (300, 40)).astype(float)
    control = calibration.calibrate_block(unlinked, variants, ["POP"] * 300, maf_threshold=.01,
                                          replicates=5, component_floor=5, seed=3)
    held_out = [r["pearson_r"] if r["accepted"] else 0.0 for r in control["replicates"] if r["panel"] == "held_out"]
    check(np.median(held_out) < .5, f"masked z-scores without LD scored as recovered: {held_out}")


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
    test_block(); test_maf_floor(); test_alid_collisions(); test_verify_block(); test_heldout_calibration()
    test_canonical_effect_allele_ld_orientation()
    test_mapping(); test_membership_keep_format(); test_amr_pilot_mapping(); test_amr_pilot_membership()
    test_index_validation(); test_compressed_pvar_input()
    test_plink_raw_input()
    for script in ("acquire_hgdp1kgp.py", "construct_block.py", "run_panels.py", "verify_block.py",
                   "heldout_calibration.py"):
        subprocess.run([sys.executable, str(SCRIPT.with_name(script)), "--help"], check=True,
                       stdout=subprocess.DEVNULL)
    print(f"ok — {checks} checks passed")

if __name__ == "__main__": main()
