#!/usr/bin/env python3
"""`tools/locus_leads_prototype_extract.py` (#444): what it refuses, and what it derives.

The extract feeds the Locus Lead report, whose every number must come from the
engine's committed outputs. These tests run it on small synthetic outputs, never
the engine's own files: an engine stand-in serves them in place of `git show`.
They check that a copy of #403's numbers that differs from #403's in any field
is refused, naming the field and both values; that an output whose sha256 is not
the one another output names is refused; that a missing key refuses rather than
writing a partial extract; and two derivations against hand-worked values.

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
            extract.build(FixtureEngine(files))
        self.assertIn("'parameters'", str(raised.exception))


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


if __name__ == "__main__":
    unittest.main()
