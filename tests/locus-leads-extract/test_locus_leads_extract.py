#!/usr/bin/env python3
"""`tools/locus_leads_prototype_extract.py` (#444): what it refuses, and what it derives.

The extract feeds the Locus Lead report, whose every number must come from the
engine's committed outputs. These tests run it on small synthetic outputs, never
the engine's own files: an engine stand-in serves them in place of `git show`.
They check that a copy of #403's numbers that differs from #403's in any field
is refused, naming the field and both values; that an output whose sha256 is not
the one another output names is refused; that a missing key refuses rather than
writing a partial extract; that the per-Analysis counts both outputs give agree
and give back the committed summary; the bp-only ceiling; and derivations
against hand-worked values.

Run from the repository root:
    python3 tests/locus-leads-extract/test_locus_leads_extract.py
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "locus_leads_prototype_extract.py"


def load_tool():
    """The extract is a script, not a module, so it is loaded by path."""
    spec = importlib.util.spec_from_file_location("locus_leads_prototype_extract", TOOL)
    assert spec is not None and spec.loader is not None, f"cannot load {TOOL}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


extract = load_tool()

D, W1, W10 = 0.5, 1_000_000, 10_000_000
POINTS = [(D, W1), ("bp_only", W1), (D, W10), ("bp_only", W10)]


class FixtureEngine(extract.Engine):
    """Serves synthetic outputs where the extract would run `git show`."""

    def __init__(self, files: dict[str, Any]) -> None:
        self.checkout = Path("fixture")
        self.commit = "f" * 40
        self.sources = []
        self._read = {}
        self.files = {name: value if isinstance(value, bytes) else json.dumps(value).encode()
                      for name, value in files.items()}

    def _git(self, *args: str) -> bytes:
        if args[0] == "show":
            return self.files[args[1].rsplit("/", 1)[1]]
        if args[0] == "log":
            return b"2026-11-05\n"
        raise AssertionError(f"unexpected git call {args}")


def sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def per_lead(leads: int, index: int, spurious: int, lost: int) -> dict[str, Any]:
    return {"analyses": 1, "locus_leads": leads, "index_variants": index, "spurious_splits": spurious,
            "lost_signals": lost, "spurious_split_rate": spurious / leads, "lost_signal_rate": lost / index,
            "by_abs_z": {"[0,20)": {"locus_leads": leads - 1, "spurious_splits": spurious,
                                    "index_variants": index - 1, "lost_signals": lost},
                         "[20,inf)": {"locus_leads": 1, "spurious_splits": 0, "index_variants": 1,
                                      "lost_signals": 0}}}


def count_row(d: Any, w: int, rho: float, ratio: float) -> dict[str, Any]:
    return {"d_cm": d, "w_bp": w, "rho": rho, "c1": rho >= 0.95, "median_ratio": ratio,
            "median_abs_log2": abs(math.log2(ratio)), "rows_median_abs_log2": 2.5,
            "analyses_without_hm3_leads": 0, "c2": 0.8 <= ratio <= 1.25, "leads_full": 10, "leads_hm3": 9,
            "share_of_leads_lost_to_density": 0.1}


def eur_outputs() -> dict[str, Any]:
    """A one-Analysis #403 selection and sweep, shaped like the committed ones."""
    selection = {"rule": {"seed": 403, "min_abs_z_100": 5, "min_p_zero": 5, "max_observed_rows": 1_000_000},
                 "pool_size": 1, "n_analyses_without_observed_rows": 0, "excluded_over_max_rows": [],
                 "chosen": [{"store": "OGS-T", "analysis_id": "A1", "max_abs_z": 120.0,
                             "stratum": "1k-10k x >=100"}]}
    rho = {(D, W1): 0.99, ("bp_only", W1): 0.98, (D, W10): 0.985, ("bp_only", W10): 0.97}
    ratio = {(D, W1): 0.85, ("bp_only", W1): 0.86, (D, W10): 0.9, ("bp_only", W10): 0.95}
    sweep = {
        "selection": {"seed": 403, "analyses": 1, "sha256": extract.selection_digest(selection)},
        "genetic_map": {"map": "EUR.tsv.gz", "sha256": "a" * 64, "assembly": "GRCh38"},
        "panel": {"path": "/panel/EUR", "assembly": "gnomAD_GRCh38"},
        "hm3": {"path": "/hm3.txt", "sha256": "b" * 64, "alids": 10},
        "grid": {"d_cm": [D], "w_bp": [W1, W10], "r2": [0.01, 0.001], "tier": 5e-08},
        "spikes": [],
        "left_out_below_shared_set_floor": {"floor": 0.7, "analyses": []},
        "evaluation": {
            "criteria": {"at_r2": "0.01", "spurious_split_rate_max": 0.1, "lost_signal_rate_max": 0.1},
            "configs": [{"d_cm": d, "w_bp": w, "by_r2": {"0.01": per_lead(10, 12, 2, 4),
                                                          "0.001": per_lead(10, 8, 5, 3)}}
                        for d, w in POINTS],
            "best": {"qualifies": False, "nearest_d_cm": D, "nearest_w_bp": W1}},
        "counts": {
            "criteria": {"C1": "rank.", "C2": "density.", "C3": "cM against bp-only.",
                         "c3_reading": "#403's reading", "rho_min": 0.95, "ratio_range": [0.8, 1.25]},
            "analyses": 1,
            "rows": {"median_ratio": 0.2, "median_abs_log2": 2.5, "rows_full": 100, "rows_hm3": 20},
            "configs": [count_row(d, w, rho[(d, w)], ratio[(d, w)]) for d, w in POINTS],
            "leads_per_index_variant": [{"d_cm": d, "w_bp": w,
                                         "z": {">=100": {"analyses": 1, "locus_leads": 10,
                                                         "index_variants": 12, "ratio": 10 / 12}}}
                                        for d, w in POINTS]},
        "analyses": [{"analysis": {"analysis_id": "A1"}, "store": "OGS-T",
                      "query": {"threshold": 5e-08, "rows": 100},
                      "rows": {"tier_rows": 100, "shared_set": 90},
                      "density": {"rows_full": 90, "rows_hm3": 20},
                      "selection": {"max_abs_z": 120.0, "z_band": ">=100"},
                      "configs": [{"d_cm": d, "w_bp": w,
                                   "by_r2": {"0.01": {"locus_leads": 10, "index_variants": 12}}}
                                  for d, w in POINTS]}],
    }
    return {"eur_sweep_selection.json": selection, "eur_sweep.json": sweep}


def profile_outputs() -> dict[str, Any]:
    """#409's output over the fixture sweep, every copy of #403's numbers exact."""
    files = eur_outputs()
    sweep, selection = files["eur_sweep.json"], files["eur_sweep_selection.json"]
    counts = sweep["counts"]

    def beside(d: Any, w: int) -> dict[str, Any]:
        row = copy.deepcopy(extract.point(counts["configs"], d, w))
        row["per_lead"] = copy.deepcopy(extract.point(sweep["evaluation"]["configs"], d, w)["by_r2"]["0.01"])
        row["leads_per_index_variant"] = copy.deepcopy(extract.point(counts["leads_per_index_variant"], d, w)["z"])
        return row

    analysis = sweep["analyses"][0]
    files["eur_profile.json"] = {
        "inputs": {"sweep_403": {"path": "eur_sweep.json", "sha256": sha256(sweep)},
                   "selection_sha256": extract.selection_digest(selection),
                   "genetic_map": copy.deepcopy(sweep["genetic_map"]), "panel": copy.deepcopy(sweep["panel"]),
                   "hm3": copy.deepcopy(sweep["hm3"])},
        "judgement": {
            "criteria": {**copy.deepcopy(counts["criteria"]), "c3_reading": "#409's own reading"},
            "analyses": 1,
            "rows": {"rows_full": 100, "rows_hm3": 20, "median_abs_log2": 2.5},
            "bp_only": [copy.deepcopy(extract.point(counts["configs"], "bp_only", w)) for w in (W1, W10)],
            "set_beside": [beside("bp_only", W10), beside(D, W1)],
            "named_analysis": {"analysis_id": "A1", "max_abs_z": 120.0, "index_variants": 12,
                               "set_beside": [{"d_cm": "bp_only", "w_bp": W10, "locus_leads": 10},
                                              {"d_cm": D, "w_bp": W1, "locus_leads": 10}],
                               "rules": []},
            "rules": [{"label": "LD blocks", "rows_median_abs_log2": 2.5}]},
        "analyses": [{"analysis": copy.deepcopy(analysis["analysis"]), "store": "OGS-T",
                      "query": {"threshold": 0.0005, "rows": 400},
                      "rows": copy.deepcopy(analysis["rows"]), "density": copy.deepcopy(analysis["density"]),
                      "selection": copy.deepcopy(analysis["selection"]), "index_variants": 12}],
        "not_swept_by_403": [],
    }
    return files


class CopiedFieldsTest(unittest.TestCase):
    """A copy of #403's numbers in a later ticket's output must equal #403's, field by field."""

    def test_the_fixture_copies_pass_the_check(self) -> None:
        files = profile_outputs()
        extract.check_profile_copies(files["eur_profile.json"], files["eur_sweep.json"],
                                     sha256(files["eur_sweep.json"]), files["eur_sweep_selection.json"])

    def test_a_set_beside_median_ratio_changed_by_one_is_refused_naming_it(self) -> None:
        files = profile_outputs()
        beside = files["eur_profile.json"]["judgement"]["set_beside"][0]
        original = beside["median_ratio"]
        beside["median_ratio"] = original + 1
        with self.assertRaises(extract.Refused) as raised:
            extract.extract_profile(FixtureEngine(files))
        message = str(raised.exception)
        self.assertIn("set_beside[0].median_ratio", message)
        self.assertIn(repr(original + 1), message)
        self.assertIn(repr(original), message)

    def test_a_copied_per_lead_rate_deep_in_a_record_is_refused(self) -> None:
        files = profile_outputs()
        band = files["eur_profile.json"]["judgement"]["set_beside"][1]["per_lead"]["by_abs_z"]["[20,inf)"]
        band["lost_signals"] += 1
        with self.assertRaises(extract.Refused) as raised:
            extract.extract_profile(FixtureEngine(files))
        self.assertIn("by_abs_z.[20,inf).lost_signals", str(raised.exception))

    def test_the_profiles_own_c3_reading_is_not_a_copy(self) -> None:
        files = profile_outputs()
        files["eur_profile.json"]["judgement"]["criteria"]["c3_reading"] = "another reading entirely"
        extract.check_profile_copies(files["eur_profile.json"], files["eur_sweep.json"],
                                     sha256(files["eur_sweep.json"]), files["eur_sweep_selection.json"])


class Sha256Test(unittest.TestCase):
    def test_the_recorded_sha256_is_of_the_bytes_served(self) -> None:
        engine = FixtureEngine({"empty.json": b"{}"})
        engine.read("empty.json", "#0")
        # sha256 of the two bytes `{}`, worked out once by hand with sha256sum.
        self.assertEqual(engine.sources[0]["sha256"],
                         "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a")

    def test_maps_naming_another_sweep_than_the_committed_one_are_refused(self) -> None:
        files = eur_outputs()
        files["eur_maps.json"] = {
            "selection": {"sha256": extract.selection_digest(files["eur_sweep_selection.json"])},
            "shared_sets_equal": True,
            "maps": {"pyrho-mean": {"sweep_sha256": "0" * 64}}}
        with self.assertRaises(extract.Refused) as raised:
            extract.extract_maps(FixtureEngine(files))
        self.assertIn("eur_sweep.json", str(raised.exception))


class MissingKeyTest(unittest.TestCase):
    def test_a_missing_key_refuses_naming_it_and_nothing_is_built(self) -> None:
        files = eur_outputs()
        files["OGS-00003_finngen-r13-BMI_IRN.json"] = {"analysis": {}}
        with self.assertRaises(extract.Refused) as raised:
            extract.build(FixtureEngine(files), FixturePanel({}))
        self.assertIn("'parameters'", str(raised.exception))


class FixturePanel(extract.Panel):
    """Serves variant-table headers where the extract would read and decompress a panel's files."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = {path: text.encode() for path, text in headers.items()}

    def _bytes(self, path: str) -> bytes:
        return self.headers[path]

    def _decompress(self, raw: bytes) -> bytes:
        return raw


def pvar_header(chromosome: str, length: int, assembly: str = "gnomAD_GRCh38") -> str:
    return (f"##hailversion=0.2\n##contig=<ID=chr{chromosome},length={length},assembly={assembly}>\n"
            f"#CHROM\tPOS\tID\tREF\tALT\n{chromosome}\t10\t{chromosome}:10:A:C\tA\tC\n")


# Three Analyses, hand-worked. Index variants 10, 20, 40. Per rule, (leads on the full shared set,
# on HapMap3 only), the rank correlation with the index variants, and the median HapMap3/full ratio
# and median |log2| of it:
#   bp-only W 10 Mb   (5, 5) (8, 4) (9, 9)      ranks agree, rho 1;   ratios 1, 0.5, 1: 1 and 0
#   D 2.5 cM, W 10 Mb (6, 3) (12, 12) (30, 15)  rho 1;                ratios 0.5, 1, 0.5: 0.5 and 1
#   B 50 kb, f 0.75   (4, 4) (9, 9) (20, 20)    rho 1;                ratios 1, 1, 1: 1 and 0
#   LD blocks         (3, 3) (2, 1) (10, 5)     ranks 2, 1, 3: rho 0.5; ratios 1, 0.5, 0.5: 0.5 and 1
INDEX = {"A1": 10, "A2": 20, "A3": 40}
BANDS = {"A1": ("<38.5", 20.0), "A2": (">=100", 150.0), "A3": ("38.5-100", 60.0)}
LEADS = {"bp_only": {"A1": (5, 5), "A2": (8, 4), "A3": (9, 9)},
         "cm": {"A1": (6, 3), "A2": (12, 12), "A3": (30, 15)},
         "prominence": {"A1": (4, 4), "A2": (9, 9), "A3": (20, 20)},
         "ld_blocks": {"A1": (3, 3), "A2": (2, 1), "A3": (10, 5)}}
COMMITTED = {"bp_only": (1.0, 1.0, 0.0), "cm": (1.0, 0.5, 1.0), "prominence": (1.0, 1.0, 0.0),
             "ld_blocks": (0.5, 0.5, 1.0)}


def counted(key: str) -> dict[str, Any]:
    rho, ratio, log2 = COMMITTED[key]
    return {"rho": rho, "c1": rho >= 0.95, "median_ratio": ratio, "median_abs_log2": log2,
            "c2": 0.8 <= ratio <= 1.25}


def counts_outputs() -> tuple[dict[str, Any], dict[str, Any]]:
    """#403's sweep and #409's profile, holding only what the per-Analysis counts read."""
    def record(key: str, analysis: str) -> dict[str, Any]:
        full, hm3 = LEADS[key][analysis]
        return {"density": {"leads_full": full, "leads_hm3": hm3},
                "by_r2": {"0.01": {"locus_leads": full, "index_variants": INDEX[analysis]}}}

    sweep = {
        "counts": {"analyses": 3,
                   "named": {"d_cm": "bp_only", "w_bp": W10}, "named_cm": {"d_cm": 2.5, "w_bp": W10},
                   "configs": [{"d_cm": "bp_only", "w_bp": W10} | counted("bp_only"),
                               {"d_cm": 2.5, "w_bp": W10} | counted("cm")]},
        "analyses": [{"store": "OGS-T", "analysis": {"analysis_id": a},
                      "selection": {"z_band": BANDS[a][0], "max_abs_z": BANDS[a][1]},
                      "configs": [{"d_cm": "bp_only", "w_bp": W10} | record("bp_only", a),
                                  {"d_cm": 2.5, "w_bp": W10} | record("cm", a)]}
                     for a in INDEX]}
    profile = {
        "judgement": {"rules": [{"label": "B 50 kb, f 0.75", "rule": "prominence"} | counted("prominence"),
                                {"label": "LD blocks (EUR)", "rule": "ld_blocks"} | counted("ld_blocks")]},
        "analyses": [{"store": "OGS-T", "analysis": {"analysis_id": a}, "index_variants": INDEX[a],
                      "rules": [{"label": "B 50 kb, f 0.75"} | record("prominence", a),
                                {"label": "LD blocks (EUR)"} | record("ld_blocks", a)]}
                     for a in reversed(list(INDEX))]}
    return sweep, profile


CEILING = {"leads": 15}


class PerAnalysisCountsTest(unittest.TestCase):
    """The figure's points come from both outputs, which must agree, and must give back the committed summary."""

    def test_the_points_give_back_the_committed_summary(self) -> None:
        sweep, profile = counts_outputs()
        counts = extract.per_analysis_counts(sweep, profile, CEILING)
        self.assertEqual([a["analysis_id"] for a in counts["analyses"]], ["A1", "A2", "A3"])
        self.assertEqual(counts["analyses"][1]["leads_full"],
                         {"bp_only": 8, "cm": 12, "prominence": 9, "ld_blocks": 2})
        self.assertEqual(counts["analyses"][1]["leads_hm3"],
                         {"bp_only": 4, "cm": 12, "prominence": 9, "ld_blocks": 1})
        self.assertEqual({m["key"]: (m["rho"], m["median_ratio"], m["median_abs_log2"]) for m in counts["methods"]},
                         COMMITTED)
        self.assertEqual(counts["z_bands"], ["<38.5", "38.5-100", ">=100"])
        self.assertEqual(counts["most_clumps"], "A3")

    def test_index_variants_that_differ_between_the_outputs_are_refused_naming_both(self) -> None:
        sweep, profile = counts_outputs()
        a2 = next(a for a in profile["analyses"] if a["analysis"]["analysis_id"] == "A2")
        a2["rules"][1]["by_r2"]["0.01"]["index_variants"] = 21
        with self.assertRaises(extract.Refused) as raised:
            extract.per_analysis_counts(sweep, profile, CEILING)
        message = str(raised.exception)
        self.assertIn("A2", message)
        self.assertIn("21", message)
        self.assertIn("20", message)

    def test_a_profile_missing_an_analysis_of_the_sweep_is_refused(self) -> None:
        sweep, profile = counts_outputs()
        profile["analyses"].pop()
        with self.assertRaises(extract.Refused) as raised:
            extract.per_analysis_counts(sweep, profile, CEILING)
        self.assertIn("A1", str(raised.exception))

    def test_a_committed_median_ratio_the_points_do_not_give_is_refused(self) -> None:
        sweep, profile = counts_outputs()
        profile["judgement"]["rules"][1]["median_ratio"] = 0.6
        with self.assertRaises(extract.Refused) as raised:
            extract.per_analysis_counts(sweep, profile, CEILING)
        message = str(raised.exception)
        self.assertIn("ld_blocks median_ratio", message)
        self.assertIn("0.5", message)
        self.assertIn("0.6", message)

    def test_a_committed_rho_the_points_do_not_give_is_refused(self) -> None:
        sweep, profile = counts_outputs()
        # A per-Analysis count changed in the sweep: A1's bp-only leads now outrank A2's and A3's.
        sweep["analyses"][0]["configs"][0]["density"]["leads_full"] = 10
        with self.assertRaises(extract.Refused) as raised:
            extract.per_analysis_counts(sweep, profile, CEILING)
        self.assertIn("bp_only rho", str(raised.exception))

    def test_a_count_a_log_axis_cannot_show_is_refused(self) -> None:
        sweep, profile = counts_outputs()
        profile["analyses"][0]["rules"][0]["density"]["leads_hm3"] = 0
        profile["judgement"]["rules"][0]["median_ratio"] = 1.0
        with self.assertRaises(extract.Refused) as raised:
            extract.per_analysis_counts(sweep, profile, CEILING)
        self.assertIn("log", str(raised.exception))

    def test_the_analyses_above_the_ceiling_by_hand(self) -> None:
        sweep, profile = counts_outputs()
        # By hand: A2 (20) and A3 (40) have more index variants than 15; bp-only finds 8 and 9 in them.
        above = extract.per_analysis_counts(sweep, profile, CEILING)["above_ceiling"]
        self.assertEqual(above, {"analyses": 2, "index_min": 20, "index_max": 40, "bp_only_min": 8, "bp_only_max": 9})

    def test_a_bp_only_count_above_the_ceiling_is_refused(self) -> None:
        sweep, profile = counts_outputs()
        with self.assertRaises(extract.Refused) as raised:
            extract.per_analysis_counts(sweep, profile, {"leads": 8})
        self.assertIn("ceiling", str(raised.exception))


class CeilingTest(unittest.TestCase):
    """bp-only leads lie more than W apart, so each chromosome's length caps their number."""

    SWEEP = {"panel": {"path": "/panel/EUR", "assembly": "gnomAD_GRCh38"},
             "genetic_map": {"points_per_chromosome": {"1": 5, "2": 5}}}

    def test_the_ceiling_by_hand(self) -> None:
        panel = FixturePanel({"/panel/EUR/chr1.pvar.zst": pvar_header("1", 25),
                              "/panel/EUR/chr2.pvar.zst": pvar_header("2", 22)})
        ceiling = extract.bp_only_ceiling(self.SWEEP, panel, 10)
        # By hand, W 10: chr1 of 25 bp holds leads at 1, 12 and 23; chr2 of 22 bp at 1 and 12 (23 is off its end).
        self.assertEqual(ceiling["leads"], 5)
        self.assertEqual([(c["chromosome"], c["length"]) for c in ceiling["contigs"]], [("1", 25), ("2", 22)])
        text = pvar_header("1", 25).encode()
        self.assertEqual(ceiling["contigs"][0]["sha256"], hashlib.sha256(text).hexdigest())
        self.assertEqual(ceiling["contigs"][0]["bytes"], len(text))

    def test_a_contig_of_another_assembly_is_refused(self) -> None:
        panel = FixturePanel({"/panel/EUR/chr1.pvar.zst": pvar_header("1", 25),
                              "/panel/EUR/chr2.pvar.zst": pvar_header("2", 22, assembly="GRCh37")})
        with self.assertRaises(extract.Refused) as raised:
            extract.bp_only_ceiling(self.SWEEP, panel, 10)
        self.assertIn("GRCh37", str(raised.exception))

    def test_a_table_of_another_chromosome_is_refused(self) -> None:
        panel = FixturePanel({"/panel/EUR/chr1.pvar.zst": pvar_header("1", 25),
                              "/panel/EUR/chr2.pvar.zst": pvar_header("3", 22)})
        with self.assertRaises(extract.Refused) as raised:
            extract.bp_only_ceiling(self.SWEEP, panel, 10)
        self.assertIn("chr3", str(raised.exception))


class DerivationTest(unittest.TestCase):
    def test_split_pairs_counts_the_far_and_the_strongly_linked(self) -> None:
        pairs = {"n": 5, "by_distance_band": {
            "<=1Mb": {"n": 2, "r2_ge_0.05": 1},
            "1-2.5Mb": {"n": 2, "r2_ge_0.05": 1},
            "2.5-10Mb": {"n": 1, "r2_ge_0.05": 0}}}
        # By hand: 2 + 1 = 3 of 5 pairs lie past 1 Mb; 1 + 1 + 0 = 2 of 5 reach r2 0.05.
        self.assertEqual(extract.split_pairs(pairs),
                         {"n": 5, "near_band_bp": 1_000_000, "beyond_1mb": 3, "beyond_1mb_share": 0.6,
                          "strong_r2": 0.05, "r2_ge_strong_share": 0.4})

    def test_the_best_log2_gain_is_over_bp_only_at_the_same_w(self) -> None:
        def row(d: Any, w: int, log2: float) -> dict[str, Any]:
            return {"d_cm": d, "w_bp": w, "median_abs_log2": log2}

        counts = {"configs": [row("bp_only", W1, 0.30), row(0.1, W1, 0.50), row(1.0, W1, 0.25),
                              row("bp_only", W10, 0.10), row(0.1, W10, 0.40), row(1.0, W10, 0.02)]}
        # By hand: at W 1 Mb the gains are -0.20 and +0.05; at W 10 Mb -0.30 and +0.08.
        self.assertAlmostEqual(extract.best_log2_gain(counts), 0.08, places=12)

    def test_spearman_averages_tied_ranks(self) -> None:
        # By hand: x ranks 1, 2.5, 2.5, 4 and y ranks 1, 3, 2, 4, both of mean 2.5. The centred
        # products sum to 4.5, the squares to 4.5 and 5, so rho = 4.5 / sqrt(22.5) = sqrt(0.9).
        self.assertAlmostEqual(extract.spearman([1, 2, 2, 3], [1, 3, 2, 4]), math.sqrt(0.9), places=12)


if __name__ == "__main__":
    unittest.main()
