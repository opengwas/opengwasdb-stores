#!/usr/bin/env python3
"""Fixture-driven tests for issues #35-40's in-repository implementation."""
from __future__ import annotations

import csv, importlib.util, json, os, shutil, subprocess, sys, tempfile
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
underresolved_spec = importlib.util.spec_from_file_location("find_underresolved_blocks", SCRIPT.with_name("find_underresolved_blocks.py"))
underresolved = importlib.util.module_from_spec(underresolved_spec); sys.modules[underresolved_spec.name] = underresolved
assert underresolved_spec.loader; underresolved_spec.loader.exec_module(underresolved)
verify_spec = importlib.util.spec_from_file_location("verify_block", SCRIPT.with_name("verify_block.py"))
verify = importlib.util.module_from_spec(verify_spec); sys.modules[verify_spec.name] = verify
assert verify_spec.loader; verify_spec.loader.exec_module(verify)
calibration_spec = importlib.util.spec_from_file_location("heldout_calibration", SCRIPT.with_name("heldout_calibration.py"))
calibration = importlib.util.module_from_spec(calibration_spec); sys.modules[calibration_spec.name] = calibration
assert calibration_spec.loader; calibration_spec.loader.exec_module(calibration)
# The runner imports its siblings by bare name, so it loads after them.
pilot_spec = importlib.util.spec_from_file_location("run_amr_pilot", SCRIPT.with_name("run_amr_pilot.py"))
pilot = importlib.util.module_from_spec(pilot_spec); sys.modules[pilot_spec.name] = pilot
assert pilot_spec.loader; pilot_spec.loader.exec_module(pilot)
panel_spec = importlib.util.spec_from_file_location("build_amr_panel", SCRIPT.with_name("build_amr_panel.py"))
panel = importlib.util.module_from_spec(panel_spec); sys.modules[panel_spec.name] = panel
assert panel_spec.loader; panel_spec.loader.exec_module(panel)

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
        np.ones(n),  # every individual heterozygous: above the floor, but no variance
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
    classes = ("n_variants_all_missing", "n_variants_invariant", "n_variants_below_floor",
               "n_variants_high_missing", "n_variants_zero_variance", "n_variants_unnameable",
               "n_variants_duplicate_record", "n_variants_alid_collision")
    check(tuple(p[c] for c in classes) == (1, 1, 1, 0, 1, 0, 0, 0), "dropped-variant classes are miscounted")
    check(sum(p[c] for c in classes) + p["n_variants_retained"] == p["n_variants_input"],
          "dropped-variant classes do not account for every input variant")
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
    # gnomAD splits multi-allelic sites, so the deletion GA>G and the insertion
    # G>GA at one position both canonicalise to ALID 1:100:G:GA (#241, #348).
    # Of the records passing every filter, the one with the higher MAF is kept.
    n = 200
    rng = np.random.default_rng(7)
    rare = np.r_[np.ones(20), np.zeros(n - 20)]  # MAC 20
    common = rng.binomial(2, .3, n).astype(float)
    g = np.column_stack([common, rare, rng.binomial(2, .3, n), rng.binomial(2, .2, n)])
    variants = [{"chrom": "1", "pos": 100, "allele1": "G", "allele2": "GA"},
                {"chrom": "1", "pos": 100, "allele1": "GA", "allele2": "G"},
                {"chrom": "1", "pos": 100, "allele1": "G", "allele2": "A"},
                {"chrom": "1", "pos": 101, "allele1": "C", "allele2": "T"}]
    result = module.construct_block(g, variants, mac_threshold=1, component_floor=1)
    table = result.variant_table
    common_mac = min(common.sum(), 2 * n - common.sum())
    check([r["SNP"] for r in table] == ["1:100:G:GA", "1:100:A:G", "1:101:C:T"] and
          table[0]["MAC"] == common_mac and (table[0]["REF"], table[0]["ALT"]) == ("G", "GA") and
          result.provenance["n_variants_alid_collision"] == 1,
          "a shared ALID must keep only its more frequent record, and name it")
    # Dosages must come from the kept record, not whichever record shares its ALID.
    _, eaf = module.standardised_ea_dosages(g, variants, table)
    check(np.allclose(eaf, [r["EAF"] for r in table], rtol=0, atol=1e-12),
          "dosages were read from the wrong record of a shared ALID")
    try:
        module.standardised_ea_dosages(g, variants, [{k: v for k, v in table[0].items() if k not in ("REF", "ALT")}])
        check(False, "an ALID naming two records was resolved without its source alleles")
    except ValueError:
        check(True, "")
    sparse = g.copy(); sparse[:60, 0] = np.nan  # the common insertion is 30% missing
    again = module.construct_block(sparse, variants, mac_threshold=1, max_missing_rate=.2, component_floor=1)
    check(again.variant_table[0]["MAC"] == 20 and again.provenance["n_variants_alid_collision"] == 0 and
          again.provenance["n_variants_high_missing"] == 1,
          "a collision is resolved among the records that pass every filter")
    # A record with no variance cannot win a collision: every called individual
    # heterozygous gives MAF 0.5, the highest possible.
    flat = g.copy(); flat[:, 1] = 1.0
    p = module.construct_block(flat, variants, mac_threshold=1, component_floor=1).provenance
    check((p["n_variants_retained"], p["n_variants_zero_variance"], p["n_variants_alid_collision"]) == (3, 1, 0),
          "a zero-variance record took its ALID's only usable record with it")
    # A record no ALID can name is excluded and counted, whether or not it is polymorphic.
    malformed = [*variants, {"chrom": "1", "pos": 102, "allele1": "C", "allele2": "C"}]
    for column in (np.zeros(n), rng.binomial(2, .3, n).astype(float)):
        built = module.construct_block(np.column_stack([g, column]), malformed, mac_threshold=1, component_floor=1)
        x, _ = module.standardised_ea_dosages(np.column_stack([g, column]), malformed, built.variant_table)
        check(x.shape == (n, 3) and built.provenance["n_variants_unnameable"] == int(column.any()),
              "an unnameable record broke construction or dosage standardisation")
    # Two identical records cannot be told apart by any reader, so neither is kept.
    twice = [*variants, {"chrom": "1", "pos": 101, "allele1": "C", "allele2": "T"}]
    built = module.construct_block(np.column_stack([g, rng.binomial(2, .2, n)]), twice, mac_threshold=1, component_floor=1)
    module.standardised_ea_dosages(np.column_stack([g, rng.binomial(2, .2, n)]), twice, built.variant_table)
    check([r["SNP"] for r in built.variant_table] == ["1:100:G:GA", "1:100:A:G"] and
          built.provenance["n_variants_duplicate_record"] == 2,
          "identical records must both be excluded")


def test_call_rate() -> None:
    # #241 owner decision: retain a variant only when at least 80% of individuals are called.
    n = 345
    rng = np.random.default_rng(80)
    def with_missing(k: int) -> np.ndarray:
        column = rng.binomial(2, .3, n).astype(float); column[:k] = np.nan; return column
    g = np.column_stack([with_missing(69), with_missing(70), with_missing(0)])  # 20.0%, 20.3%, 0%
    variants = [{"chrom": "1", "pos": 100 + i, "allele1": "C", "allele2": "G"} for i in range(3)]
    p = module.construct_block(g, variants, maf_threshold=.01, max_missing_rate=.2, component_floor=1).provenance
    check(p["n_variants_retained"] == 2 and p["n_variants_high_missing"] == 1 and p["max_missing_rate"] == .2,
          "the call-rate ceiling is not inclusive at exactly 80% called")
    unfiltered = module.construct_block(g, variants, maf_threshold=.01, component_floor=1).provenance
    check(unfiltered["n_variants_retained"] == 3 and unfiltered["max_missing_rate"] is None,
          "without a ceiling, missingness must filter nothing")
    command = materialize.conversion_command(Path("plink2"), Path("chr1.vcf.bgz"), Path("AMR.keep"),
                                             Path("AMR/chr1"), None, pass_only=True)
    check("--var-filter" in command and "--var-filter" not in materialize.conversion_command(
              Path("plink2"), Path("chr1.vcf.bgz"), Path("AFR.keep"), Path("AFR/chr1"), 50),
          "PASS-only must be opt-in at conversion")


def test_run_panels_floor_resume() -> None:
    g, variants = ld_fixture()
    built = module.construct_block(g, variants, mac_threshold=50, component_floor=5)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp); module._atomic_outputs(tmp / "panel/AMR/22/100-200", built)
        (tmp / "regions.tsv").write_text("chr\tstart\tend\tancestry\n22\t100\t200\tAMR\n")
        def run(*floor: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, str(SCRIPT.with_name("run_panels.py")),
                "--genotype-root", str(tmp), "--reference-root", str(tmp / "panel"),
                "--regions", str(tmp / "regions.tsv"), "--members", str(tmp), "--ancestries", "AMR", *floor],
                capture_output=True, text=True)
        same = run("--mac", "50")
        check(same.returncode == 0 and "built" not in same.stdout, "a block already built under the floor was rebuilt")
        other = run("--maf", ".01")
        check(other.returncode != 0 and "floor" in other.stderr,
              "a block built under another floor must not be resumed as if it were current")
        ceiling = run("--mac", "50", "--max-missing", ".2")
        check(ceiling.returncode != 0 and "floor" in ceiling.stderr,
              "a block built without a missingness ceiling must not be resumed under one")
        # A block from before the ALID-collision rule and source alleles is not current either.
        provenance = tmp / "panel/AMR/22/100-200.provenance.json"
        old = json.loads(provenance.read_text()); old.pop("alid_collision_rule"); provenance.write_text(json.dumps(old))
        legacy = run("--mac", "50")
        check(legacy.returncode != 0 and "floor" in legacy.stderr,
              "a block built before the ALID-collision rule was resumed as current")


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
        def same_alleles(base, arrays, provenance):
            lines = base.with_suffix(".tsv").read_text().splitlines(keepends=True)
            fields = lines[1].rstrip("\n").split("\t"); fields[-1] = fields[-2]  # ALT = REF
            lines[1] = "\t".join(fields) + "\n"; base.with_suffix(".tsv").write_text("".join(lines))
        check("binding" in failures(swap_rows), "permuted rows passed the binding check")
        check("binding" in failures(flip_row), "a flipped allele passed the binding check")
        check("finite" in failures(nan), "a non-finite eigenvector passed")
        check("non_negative" in failures(negative), "a negative eigenvalue passed")
        check(failures(under_resolved) == ["resolved_at_thresh"], "an under-resolved block passed")
        check("alids" in failures(same_alleles), "a row whose source alleles name no variant passed")


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


def test_pair_records() -> None:
    # #348: the split can keep a different record of one ALID on each side, so
    # the halves share no dosage to standardise. It pairs nothing and is counted.
    train = [{"SNP": "1:100:G:GA", "REF": "G", "ALT": "GA"},
             {"SNP": "1:101:C:T", "REF": "C", "ALT": "T"}]
    test = [{"SNP": "1:100:G:GA", "REF": "GA", "ALT": "G"},
            {"SNP": "1:101:C:T", "REF": "C", "ALT": "T"}]
    shared, mismatched = calibration.pair_records(train, test)
    check(shared == [train[1]] and mismatched == 1,
          f"an ALID the halves kept different records of must be counted, not shared: {shared}, {mismatched}")


def test_panel_summary() -> None:
    # #354: a panel's summary must name every block the panel declares -- not
    # just the chromosomes this invocation touched -- and must not credit a
    # verification report written before the block it verified.
    g, variants = ld_fixture()
    built = module.construct_block(g, variants, mac_threshold=1, component_floor=5)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        for name in ("22/100-200", "22/300-400"):
            module._atomic_outputs(out / "panel/AMR" / name, built)
            (out / "verify").mkdir(exist_ok=True)
            (out / "verify" / f"{name.replace('/', '_')}.json").write_text(json.dumps({"failures": []}))
        def provenance(name: str) -> float:
            return (out / "panel/AMR" / name).with_suffix(".provenance.json").stat().st_mtime
        fresh, stale = out / "verify/22_100-200.json", out / "verify/22_300-400.json"
        os.utime(fresh, (provenance("22/100-200") + 60, provenance("22/100-200") + 60))
        os.utime(stale, (provenance("22/300-400") - 60, provenance("22/300-400") - 60))
        (out / "panel/failures.tsv").write_text(
            "AMR/22/500-600\t1\tSystemExit: no variants remain after filtering\n")
        regions = out / "regions.tsv"
        regions.write_text("chr\tstart\tend\tancestry\n22\t100\t200\tAMR\n22\t300\t400\tAMR\n"
                           "22\t500\t600\tAMR\n22\t700\t800\tAMR\n")
        run = out / "runs/20261022T000000Z"
        summary = panel.panel_summary(out, regions, [{"chr": "22", "start": "500", "end": "600"}], run,
                                      maf=.01, max_missing=.2, n_individuals=g.shape[0])
        blocks = summary["blocks"]
        check(sorted(blocks) == ["22_100-200", "22_300-400", "22_500-600", "22_700-800"] and
              summary["blocks_declared"] == 4,
              f"the summary must name every declared block: {sorted(blocks)}")
        check(blocks["22_100-200"]["status"] == "built" and blocks["22_100-200"]["verification_failures"] == [],
              "a block verified clean after it was built was not reported clean")
        check(blocks["22_300-400"]["status"] == "built" and
              blocks["22_300-400"]["verification_failures"] == ["not_verified"],
              "a verification report older than the block it verified was credited")
        check(blocks["22_500-600"]["status"] == "empty", "a block with no variants left must read as empty")
        check(blocks["22_700-800"] == {"status": "failed", "error": "not built"},
              "a declared block with neither outputs nor an error must read as failed")
        # A calibration step that wrote no result is a failure, not an absence.
        check(summary["calibration"] == {"22_500-600": {"failed": str(run / "logs/calibrate_22_500-600.log")}},
              f"a missing calibration result must be reported as failed: {summary['calibration']}")


def test_conversion_markers() -> None:
    # #354: a converted chromosome records what it was converted from, so a
    # rerun can refuse to overwrite it. Markers written before this change name
    # PASS-only and MAC alone; membership was frozen separately, so the legacy
    # text says nothing about which members, and that is not a mismatch.
    sha, other = "2b6cc790670b0038b3c111a5b223071540ffbaac32c622f59338d1ca13ad67a0", "f" * 64
    current = panel.conversion_marker(True, 1, sha)
    check(current.count("\n") == 1 and current.endswith("\n"), "a marker must be one JSON line")
    check(json.loads(current) == {"pass_only": True, "mac": 1, "members_sha256": sha},
          f"the marker does not name what it converted: {json.loads(current)}")
    check(panel.marker_matches(current, panel.conversion_marker(True, 1, sha)),
          "a marker must match itself")
    legacy = "plink2 PASS-only biallelic conversion of the AMR members; MAC >= 1"
    check(panel.marker_matches(legacy, panel.conversion_marker(True, 1, sha)) and
          panel.marker_matches(legacy, panel.conversion_marker(True, 1, other)),
          "the legacy marker must match a PASS-only, MAC >= 1 conversion whatever the members hash")
    check(not panel.marker_matches(legacy, panel.conversion_marker(True, 50, sha)),
          "the legacy text names MAC >= 1 and must not match a MAC >= 50 conversion")
    check(not panel.marker_matches(panel.conversion_marker(False, 1, sha), panel.conversion_marker(True, 1, sha)),
          "a conversion that kept every record must not match a PASS-only one")
    check(not panel.marker_matches(panel.conversion_marker(True, 1, other), current),
          "a marker naming other members must not match")


def test_run_all_stops_on_first_failure() -> None:
    # #354: a failing stage must stop the queue, not drain it. A subprocess
    # already reading a chromosome finishes; one not yet started never runs.
    ran: list[int] = []
    def fail_first(item: int) -> None:
        ran.append(item)
        if item == 0: raise RuntimeError("conversion failed")
    try:
        panel.run_all(fail_first, [0, 1, 2, 3], workers=1)
        check(False, "run_all swallowed the first failure")
    except RuntimeError as exc:
        check(str(exc) == "conversion failed", f"run_all raised a different exception: {exc}")
    check(ran == [0], f"a queued item ran after the first failure: {ran}")
    done: list[int] = []
    panel.run_all(done.append, [9, 10, 11, 12], workers=2)
    check(sorted(done) == [9, 10, 11, 12], f"run_all did not run every item: {done}")


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


AMR_LD = ROOT / "resources/reference-resources/hgdp1kgp-hg38-amr-ld"


def test_amr_regions() -> None:
    # The genome-wide AMR panel reuses the EUR block layout, relabelled AMR (#241).
    def rows(path: Path) -> list[dict[str, str]]: return list(csv.DictReader(path.open(), delimiter="\t"))
    eur = [(r["chr"], r["start"], r["end"]) for r in rows(AMR_LD.parent / "hgdp1kgp-hg38-ld/ld_regions_hg38.tsv")
           if r["ancestry"] == "EUR"]
    amr = rows(AMR_LD / "ld_regions_hg38.amr.tsv")
    check(len(eur) == 1357 and [(r["chr"], r["start"], r["end"]) for r in amr] == eur and
          all(r["ancestry"] == "AMR" and r["boundary_layout"] == "EUR" for r in amr),
          "the AMR layout must be exactly the EUR blocks, relabelled AMR")


def test_amr_pilot_mapping() -> None:
    def table(path: Path) -> list[dict[str, str]]: return list(csv.DictReader(path.open(), delimiter="\t"))
    production = {r["population"] for r in table(AMR_LD.parent / "hgdp1kgp-hg38-ld/populations_to_superpop.tsv")}
    pilot = {r["population"]: r for r in table(AMR_LD / "populations_to_superpop.tsv")}
    included = {p for p, r in pilot.items() if r["status"] == "included"}
    check(set(pilot) == production, "the pilot mapping must label exactly the production populations")
    check(included == {"CLM", "MXL", "PEL", "PUR"} and all(pilot[p]["panel"] == "AMR" for p in included),
          "the pilot includes only the four 1kGP AMR populations")
    check(all(pilot[p]["reason"] == "hgdp_excluded_amr_pilot"
              for p in ("Pima", "Maya", "Karitiana", "Surui", "Colombian")),
          "HGDP American populations must be excluded by name")
    check(all(r["status"] == "excluded" and not r["panel"] and r["reason"]
              for p, r in pilot.items() if p not in included), "population is neither mapped nor excluded")
    samples = table(AMR_LD / "phase3-amr-samples.tsv")
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
    test_block(); test_maf_floor(); test_alid_collisions(); test_call_rate(); test_run_panels_floor_resume()
    test_verify_block(); test_heldout_calibration(); test_pair_records(); test_panel_summary()
    test_conversion_markers(); test_run_all_stops_on_first_failure()
    test_canonical_effect_allele_ld_orientation()
    test_mapping(); test_membership_keep_format(); test_amr_regions(); test_amr_pilot_mapping(); test_amr_pilot_membership()
    test_index_validation(); test_compressed_pvar_input()
    test_plink_raw_input()
    for script in ("acquire_hgdp1kgp.py", "construct_block.py", "run_panels.py", "verify_block.py",
                   "heldout_calibration.py", "boundary_ld.py", "run_amr_pilot.py", "build_amr_panel.py"):
        subprocess.run([sys.executable, str(SCRIPT.with_name(script)), "--help"], check=True,
                       stdout=subprocess.DEVNULL)
    print(f"ok — {checks} checks passed")

if __name__ == "__main__": main()
