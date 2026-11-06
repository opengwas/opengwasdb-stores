#!/usr/bin/env python3
"""Extract the numbers the Locus Lead prototype report shows (issue #444).

The report, `docs/reports/locus-leads-prototype.qmd`, reads only the file this
writes. Every value in it comes from the prototype outputs the engine committed
under `docs/benchmark-output/locus_leads/` (opengwas/opengwasdb#402-#406, #409),
read at one commit with `git show`, never from a working tree. Every derived
number -- a sum, a range, a share, a ratio, a difference -- is computed here, not
in the report and not by hand. The file records the engine commit and the
sha256 of each output it read, and a re-run at the same commit reproduces it
byte for byte.

Where two outputs record the same quantity -- #403's counts as #404 and #409
reproduce them, the sweep and selection digests each later ticket names, a
panel's sample count -- the extract checks that they agree, and refuses to write
when they do not.

Run from the repository root:

    pixi run -e dev python3 tools/locus_leads_prototype_extract.py \\
        --engine <an opengwasdb checkout holding the commit> --commit 06d1a43 \\
        --output docs/data/locus-leads-prototype.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

OUTPUTS = "docs/benchmark-output/locus_leads"
BP_ONLY = "bp_only"
R2_PRIMARY = "0.01"
R2_LEGACY = "0.001"
# The max-|z| bands the owner's decision on opengwas/opengwasdb#403 quotes for
# lost signals by peak strength: below 20, and at or above 100.
WEAK_PEAK_BELOW = 20.0
STRONG_PEAK_FROM = 100.0
# #403's tracer configuration, the one #406 timed and #409 sets its rules beside.
TRACER_POINT = (0.5, 1_000_000)
# #403's named point, and the window the owner chose for `n_loci_*`.
NAMED_POINT = (BP_ONLY, 10_000_000)
MAPS = ("pyrho-mean", "pyrho-median", "hapmap2")
ANCESTRIES = ("EUR", "AFR", "EAS", "AMR")
NEAR_BAND = "<=1Mb"
# A disagreeing pair this strongly linked is far above what chance gives at the panels' sizes.
STRONG_R2 = "0.05"
# The Analysis #403's decision and #409 single out: the strongest peak's shadows.
NAMED_ANALYSIS = "GCST90162563"


class Refused(SystemExit):
    """Two outputs disagree, or one is not what the report assumes."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Refused(f"refused: {message}")


class Engine:
    """The engine's committed outputs at one commit, each read once and recorded."""

    def __init__(self, checkout: Path, commit: str) -> None:
        self.checkout = checkout
        self.commit = self._git("rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
        self.sources: list[dict[str, Any]] = []
        self._read: dict[str, tuple[Any, str]] = {}

    def _git(self, *args: str) -> bytes:
        return subprocess.run(["git", "-C", str(self.checkout), *args], check=True,
                              capture_output=True).stdout

    def read(self, name: str, ticket: str) -> tuple[Any, str]:
        """One output's parsed JSON and the sha256 of its committed bytes."""
        if name not in self._read:
            path = f"{OUTPUTS}/{name}"
            raw = self._git("show", f"{self.commit}:{path}")
            digest = hashlib.sha256(raw).hexdigest()
            self.sources.append({"path": path, "ticket": ticket, "bytes": len(raw), "sha256": digest})
            self._read[name] = (json.loads(raw), digest)
        return self._read[name]

    def committed_on(self, name: str) -> str:
        """The date of the last commit that changed an output, as of this commit."""
        return self._git("log", "-1", "--format=%cs", self.commit, "--",
                         f"{OUTPUTS}/{name}").decode().strip()


def d_out(d: Any) -> float | None:
    """D as the extract writes it: a number, or null for the bp-only rule."""
    return None if d == BP_ONLY else d


def point(rows: list[dict[str, Any]], d: Any, w: int) -> dict[str, Any]:
    found = [row for row in rows if row["d_cm"] == d and row["w_bp"] == w]
    require(len(found) == 1, f"expected one record at D {d}, W {w}, found {len(found)}")
    return found[0]


def selection_digest(selection: dict[str, Any]) -> str:
    """How a sweep names the selection it ran on (`sweep.py`: sorted-key JSON, sha256)."""
    return hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest()


def p_zero_abs_z() -> float:
    """The smallest |z|, to 0.01, at which a float64 two-sided p underflows to 0."""
    for hundredths in range(3000, 5000):
        z = hundredths / 100
        if math.erfc(z / math.sqrt(2)) == 0.0:
            return z
    raise Refused("refused: no underflow below |z| 50")


def strongest_band(by_abs_z: dict[str, dict[str, int]]) -> tuple[str, dict[str, int]]:
    """The last |z| band of a per-lead record: the leads at the strongest peaks."""
    band = list(by_abs_z)[-1]
    require(band.endswith(",inf)"), f"the last |z| band is {band}, not an open one")
    return band, by_abs_z[band]


def band_floor(band: str) -> float:
    """The lower edge of a `[lo,hi)` |z| band, as the engine labels it."""
    return float(band[1:].split(",")[0])


def distance_band_bp(band: str) -> int:
    """The edge of the engine's nearest distance band, `<=1Mb`, in bp."""
    match = re.fullmatch(r"<=(\d+(?:\.\d+)?)Mb", band)
    require(match is not None, f"unexpected distance band {band}")
    return int(float(match.group(1)) * 1_000_000)


def over_abs_z(rows: dict[str, Any]) -> int:
    """The |z| a `rows_abs_z_over_<n>` field counts past, read from the field's own name."""
    keys = [key for key in rows if key.startswith("rows_abs_z_over_")]
    require(len(keys) == 1, f"expected one rows_abs_z_over_<n> field, found {keys}")
    return int(keys[0].rsplit("_", 1)[1])


def dtype_bytes(dtype: str) -> int:
    """Bytes per value of a numpy dtype name such as `float32`."""
    match = re.fullmatch(r"[a-z]+(\d+)", dtype)
    require(match is not None, f"unexpected dtype {dtype}")
    return int(match.group(1)) // 8



def band_disagreement(counts: dict[str, int]) -> float:
    return ((counts["spurious_splits"] + counts["lost_signals"])
            / (counts["locus_leads"] + counts["index_variants"]))


def rates(record: dict[str, Any]) -> dict[str, float]:
    """A per-lead record's two rates."""
    return {"spurious_split_rate": record["spurious_split_rate"], "lost_signal_rate": record["lost_signal_rate"]}


def counts_summary(counts: dict[str, Any]) -> dict[str, Any]:
    """C1-C3 over one sweep's points, with every count and range the report states."""
    rows = counts["configs"]
    rho = [row["rho"] for row in rows]
    c3 = []
    for entry in counts["c3"]:
        w = entry["w_bp"]
        bp = point(rows, BP_ONLY, w)
        deltas = []
        for row in rows:
            if row["w_bp"] != w or row["d_cm"] == BP_ONLY:
                continue
            gain_log2 = (None if row["median_abs_log2"] is None or bp["median_abs_log2"] is None
                         else bp["median_abs_log2"] - row["median_abs_log2"])
            deltas.append({"d_cm": row["d_cm"], "delta_rho": row["rho"] - bp["rho"],
                           "delta_abs_log2": gain_log2})
        c3.append({"w_bp": w, "holds": entry["holds"], "d_cm_that_hold": entry["d_cm_that_holds"],
                   "bp_only_rho": entry["bp_only_rho"],
                   "bp_only_median_abs_log2": entry["bp_only_median_abs_log2"],
                   "largest_rho_gain": max(deltas, key=lambda x: x["delta_rho"])})
    largest = max((dict(entry["largest_rho_gain"], w_bp=entry["w_bp"]) for entry in c3),
                  key=lambda x: x["delta_rho"])
    named = counts["named"]
    named_cm = counts["named_cm"]
    return {
        "analyses": counts["analyses"],
        "points": len(rows),
        "rows": {"median_ratio": counts["rows"]["median_ratio"],
                 "median_abs_log2": counts["rows"]["median_abs_log2"],
                 "c2_abs_log2_limit": counts["rows"]["median_abs_log2"]
                 * counts["criteria"]["log2_at_most_this_share_of_rows"]},
        "c1_met": sum(row["c1"] for row in rows),
        "c2_met": sum(row["c2"] for row in rows),
        "both_met": sum(row["c1"] and row["c2"] for row in rows),
        "rho_min": min(rho),
        "rho_max": max(rho),
        "c1_holds_at_d": sorted({d_out(row["d_cm"]) for row in rows if row["c1"]},
                                key=lambda d: math.inf if d is None else d),
        "c2_fails_at_d": sorted({d_out(row["d_cm"]) for row in rows if not row["c2"]},
                                key=lambda d: math.inf if d is None else d),
        "named": None if named is None else {"d_cm": d_out(named["d_cm"]), "w_bp": named["w_bp"],
                                             "lost_to_density": named["share_of_leads_lost_to_density"]},
        "named_cm": None if named_cm is None else {
            "d_cm": named_cm["d_cm"], "w_bp": named_cm["w_bp"],
            "lost_to_density": named_cm["share_of_leads_lost_to_density"]},
        "c3": c3,
        "c3_holds_at_w": [entry["w_bp"] for entry in c3 if entry["holds"]],
        "largest_rho_gain": largest,
        "best_abs_log2_gain": best_log2_gain(counts),
        "configs": [{"d_cm": d_out(row["d_cm"]), "w_bp": row["w_bp"], "rho": row["rho"],
                     "median_ratio": row["median_ratio"], "median_abs_log2": row["median_abs_log2"],
                     "lost_to_density": row["share_of_leads_lost_to_density"]} for row in rows],
    }


def best_log2_gain(counts: dict[str, Any]) -> float:
    """The largest improvement in median |log2| any cM point makes over bp-only at its W."""
    rows = counts["configs"]
    gains = []
    for row in rows:
        if row["d_cm"] == BP_ONLY:
            continue
        bp = point(rows, BP_ONLY, row["w_bp"])
        if row["median_abs_log2"] is not None and bp["median_abs_log2"] is not None:
            gains.append(bp["median_abs_log2"] - row["median_abs_log2"])
    return max(gains)


def split_pairs(pairs: dict[str, Any]) -> dict[str, Any]:
    """How far a set of disagreeing pairs reach, and how many sit well above chance LD."""
    bands = pairs["by_distance_band"]
    n = pairs["n"]
    beyond = sum(counts["n"] for band, counts in bands.items() if band != NEAR_BAND)
    strong = sum(counts[f"r2_ge_{STRONG_R2}"] for counts in bands.values())
    return {"n": n, "near_band_bp": distance_band_bp(NEAR_BAND),
            "beyond_1mb": beyond, "beyond_1mb_share": beyond / n if n else None,
            "strong_r2": float(STRONG_R2), "r2_ge_strong_share": strong / n if n else None}


# --------------------------------------------------------------------------- tickets


def extract_method(engine: Engine) -> dict[str, Any]:
    tracer, _ = engine.read("OGS-00003_finngen-r13-BMI_IRN.json", "#402")
    sweep, _ = engine.read("eur_sweep.json", "#403")
    grid = sweep["grid"]
    params = tracer["parameters"]
    evaluation = sweep["evaluation"]["criteria"]
    criteria = sweep["counts"]["criteria"]
    tier = grid["tier"]
    return {
        "grid": {"d_cm": grid["d_cm"], "w_bp": grid["w_bp"], "r2": grid["r2"], "tier": tier},
        "clump": {"kb": params["clump_kb"], "p1": params["clump_p1"], "r2_mode": params["clump_r2"]},
        "p_zero_abs_z": p_zero_abs_z(),
        "z_at_tier": statistics.NormalDist().inv_cdf(1 - tier / 2),
        "map": {"record": tracer["genetic_map"]["archive"]["record"],
                "md5": tracer["genetic_map"]["archive"]["md5"]},
        "definitions": tracer["definitions"],
        "per_lead_criteria": {
            "at_r2": evaluation["at_r2"],
            "spurious_split_rate_max": evaluation["spurious_split_rate_max"],
            "lost_signal_rate_max": evaluation["lost_signal_rate_max"],
            "clearly_better_pooled_at_most": evaluation["clearly_better"]["pooled_rate_at_most"],
            "clearly_better_analyses_lower_at_least": evaluation["clearly_better"]["analyses_lower_at_least"],
        },
        "count_criteria": {key: criteria[key] for key in (
            "C1", "C2", "C3", "c3_reading", "named", "rho_min", "ratio_range", "c3_margin_rho",
            "c3_margin_abs_log2")},
        "hm3": {"alids": sweep["hm3"]["alids"]},
    }


def extract_tracer(engine: Engine) -> dict[str, Any]:
    tracer, _ = engine.read("OGS-00003_finngen-r13-BMI_IRN.json", "#402")
    rows = tracer["rows"]
    partition = rows["partition"]
    require(sum(partition.values()) == rows["tier_rows"], "#402's partition does not add up to its tier")
    comparison = tracer["comparison"]
    by_r2 = {r2: {key: record[key] for key in ("locus_leads", "index_variants", "agreed", "spurious_splits",
                                               "lost_signals", "spurious_split_rate", "lost_signal_rate")}
             for r2, record in comparison["by_r2"].items()}
    w = comparison["w_bp"]
    distances = [pair["distance_bp"] for pair in comparison["by_r2"][R2_PRIMARY]["spurious_split_pairs"]]
    return {
        "store": tracer["store"]["store_id"],
        "analysis_id": tracer["analysis"]["analysis_id"],
        "analysis_label": tracer["analysis"]["analysis_label"],
        "d_cm": comparison["d_cm"],
        "w_bp": w,
        "tier_rows": rows["tier_rows"],
        "imputed": tracer["query"]["imputed"],
        "partition": partition,
        "shared_set_share": rows["shared_set_share"],
        "ties": {"tied_rows": tracer["ranking"]["tied_rows"], "tied_groups": tracer["ranking"]["tied_groups"]},
        "by_r2": by_r2,
        "splits_beyond_w": sum(distance > w for distance in distances),
        "splits_median_distance_bp": statistics.median(distances),
    }


def extract_eur(engine: Engine) -> dict[str, Any]:
    sweep, _ = engine.read("eur_sweep.json", "#403")
    selection, _ = engine.read("eur_sweep_selection.json", "#403")
    require(sweep["selection"]["sha256"] == selection_digest(selection),
            "eur_sweep.json ran on another selection than eur_sweep_selection.json")
    rule = selection["rule"]
    # The |z| quota is named for its edge (`min_abs_z_100`); read the edge from the name.
    quota_keys = [key for key in rule if key.startswith("min_abs_z_")]
    require(len(quota_keys) == 1, f"expected one min_abs_z_<n> quota, found {quota_keys}")
    quota_key = quota_keys[0]
    quota_from = int(quota_key.rsplit("_", 1)[1])
    analyses = sweep["analyses"]
    below = sweep["left_out_below_shared_set_floor"]
    tier = sum(a["rows"]["tier_rows"] for a in analyses)
    shared = sum(a["rows"]["shared_set"] for a in analyses)

    # Per-lead diagnostics.
    evaluation = sweep["evaluation"]
    configs = evaluation["configs"]
    best = evaluation["best"]
    require(not best["qualifies"], "#403 now names a qualifying per-lead point; the report says none does")
    nearest = (best["nearest_d_cm"], best["nearest_w_bp"])
    points = [{"d_cm": d_out(c["d_cm"]), "w_bp": c["w_bp"]} | rates(c["by_r2"][R2_PRIMARY]) for c in configs]
    criteria = evaluation["criteria"]
    low_spurious = [p for p in points if p["spurious_split_rate"] <= criteria["spurious_split_rate_max"]]
    near = point(configs, *nearest)["by_r2"][R2_PRIMARY]
    near_bp = point(configs, BP_ONLY, nearest[1])["by_r2"][R2_PRIMARY]
    spikes = [{"kind": kind, "n": near[kind]["n"], "straddling": near[kind]["straddling_a_spike"]}
              for kind in ("spurious_split_pairs", "lost_signal_pairs")]

    # Per-Analysis lost signals by peak strength, the figure's rows.
    per_analysis = []
    for a in analyses:
        tp = point(a["configs"], *TRACER_POINT)["by_r2"][R2_PRIMARY]
        per_analysis.append({
            "analysis_id": a["analysis"]["analysis_id"], "max_abs_z": a["selection"]["max_abs_z"],
            "index_variants": tp["index_variants"], "locus_leads": tp["locus_leads"],
            "lost_rate_tracer_point": tp["lost_signals"] / tp["index_variants"] if tp["index_variants"] else None,
        })
    weak = [a["lost_rate_tracer_point"] for a in per_analysis if a["max_abs_z"] < WEAK_PEAK_BELOW]
    strong = [a["lost_rate_tracer_point"] for a in per_analysis if a["max_abs_z"] >= STRONG_PEAK_FROM]
    named = next(a for a in per_analysis if a["analysis_id"] == NAMED_ANALYSIS)
    chance = sweep["chance_ld"]
    return {
        "selection": {
            "seed": rule["seed"], "quota_z": rule[quota_key], "quota_z_from": quota_from,
            "quota_p_zero": rule["min_p_zero"], "max_observed_rows": rule["max_observed_rows"],
            "pool": selection["pool_size"], "no_observed_rows": selection["n_analyses_without_observed_rows"],
            "drawn": len(selection["chosen"]),
            "excluded_over_max_rows": [{"analysis_id": e["analysis_id"], "max_abs_z": e["max_abs_z"]}
                                       for e in selection["excluded_over_max_rows"]],
            "committed_on": engine.committed_on("eur_sweep_selection.json"),
        },
        "swept": len(analyses),
        "swept_strong": sum(a["selection"]["z_band"] == f">={quota_from}" for a in analyses),
        "below_floor": {"floor": below["floor"],
                        "analyses": [{"analysis_id": b["analysis"]["analysis_id"],
                                      "shared_set_share": b["shared_set_share"]} for b in below["analyses"]]},
        "rows": {"tier": tier, "shared_share": shared / tier},
        "per_lead": {
            "points": points,
            "qualifying": sum(1 for c in configs if c.get("qualifies_pooled") or c.get("qualifies_median")),
            "nearest": {"d_cm": nearest[0], "w_bp": nearest[1]} | rates(near),
            "nearest_bp_only": rates(near_bp),
            "lost_rate_min": min(p["lost_signal_rate"] for p in points),
            "low_spurious_points": len(low_spurious),
            "low_spurious_w_min": min(p["w_bp"] for p in low_spurious),
            "low_spurious_lost_min": min(p["lost_signal_rate"] for p in low_spurious),
            "nearest_splits": split_pairs(near["spurious_split_pairs"]),
            "nearest_spikes": spikes,
            "chance_index_pairs": {"pairs_evaluable": chance["pairs_evaluable"],
                                   "share_r2_ge_0.001": chance["share_r2_ge_0.001"],
                                   "share_r2_ge_0.01": chance["share_r2_ge_0.01"]},
            "by_peak": {"weak_below": WEAK_PEAK_BELOW, "strong_from": STRONG_PEAK_FROM,
                        "weak_n": len(weak), "weak_min": min(weak), "weak_max": max(weak),
                        "strong_n": len(strong), "strong_min": min(strong), "strong_max": max(strong)},
            "named_analysis": {
                "analysis_id": NAMED_ANALYSIS, "max_abs_z": named["max_abs_z"],
                "locus_leads": named["locus_leads"], "index_variants": named["index_variants"],
                "expected_z_at_legacy_r2": named["max_abs_z"] * math.sqrt(float(R2_LEGACY))},
        },
        "per_analysis": [{key: a[key] for key in ("analysis_id", "max_abs_z", "index_variants",
                                                   "lost_rate_tracer_point")} for a in per_analysis],
        "spikes": [{"population": s["population"], "chromosome": s["chromosome"], "position": s["position"],
                    "interval_bp": s["interval_bp"], "population_cm": s["population_cm"],
                    "mean_map_cm": s["mean_map_cm"]} for s in sweep["spikes"]],
        "counts": counts_summary(sweep["counts"]),
    }


def extract_maps(engine: Engine) -> dict[str, Any]:
    maps, _ = engine.read("eur_maps.json", "#404")
    sweep, sweep_sha = engine.read("eur_sweep.json", "#403")
    selection, _ = engine.read("eur_sweep_selection.json", "#403")
    mean = maps["maps"]["pyrho-mean"]
    require(mean["sweep_sha256"] == sweep_sha, "#404's pyrho-mean sweep is not #403's committed eur_sweep.json")
    require(mean["counts"] == sweep["counts"], "#404's pyrho-mean counts differ from #403's")
    require(maps["selection"]["sha256"] == selection_digest(selection), "#404 ran on another selection")
    require(maps["shared_sets_equal"], "#404's maps saw different shared sets")
    out = []
    for name in MAPS:
        record = maps["maps"][name]
        summary = counts_summary(record["counts"])
        at_tracer = point(record["per_lead"], *TRACER_POINT)["by_r2"][R2_PRIMARY]
        dropped = record["genetic_map"].get("dropped_to_a_non_decreasing_cM")
        out.append({
            "name": name, "total_cm": maps["map_measurements"][name]["total_cM"],
            "spike_gain_cm": [{"chromosome": s["chromosome"], "cm_gain": s["cm_gain"]}
                              for s in maps["map_measurements"][name]["spike_intervals"]],
            "clamped": record["shared_set"]["clamped"],
            "points_dropped": None if dropped is None else sum(dropped["points_dropped"].values()),
            "c1_met": summary["c1_met"], "c2_met": summary["c2_met"], "points_judged": summary["points"],
            "c3_holds_at_w": summary["c3_holds_at_w"],
            "named": summary["named"], "named_cm": summary["named_cm"],
            "largest_rho_gain": summary["largest_rho_gain"]["delta_rho"],
            "tracer_point": rates(at_tracer),
        })
    named = {(m["named"]["d_cm"], m["named"]["w_bp"]) if m["named"] else None for m in out}
    return {
        "maps": out,
        "same_named_point": len(named) == 1,
        "c3_nowhere": all(not m["c3_holds_at_w"] for m in out),
        "largest_rho_gain_map": max(out, key=lambda m: m["largest_rho_gain"])["name"],
        "shared_set": {"rows": mean["shared_set"]["shared_set_rows"], "tier_rows": mean["shared_set"]["tier_rows"],
                       "share": mean["shared_set"]["shared_set_rows"] / mean["shared_set"]["tier_rows"],
                       "analyses_checked": mean["shared_set_checked"]["analyses"]},
        "hapmap2_length_over_mean": out[MAPS.index("hapmap2")]["total_cm"] / out[MAPS.index("pyrho-mean")]["total_cm"],
    }


def extract_ancestries(engine: Engine, eur: dict[str, Any]) -> dict[str, Any]:
    chance, _ = engine.read("ancestries_chance_ld.json", "#405")
    tracer, _ = engine.read("OGS-00003_finngen-r13-BMI_IRN.json", "#402")
    out = {}
    raw_rows = {}
    for ancestry in ANCESTRIES:
        if ancestry == "EUR":
            sweep_name, selection_name, ticket = "eur_sweep.json", "eur_sweep_selection.json", "#403"
        else:
            stem = f"ancestries_{ancestry.lower()}"
            sweep_name, selection_name, ticket = f"{stem}_sweep.json", f"{stem}_selection.json", "#405"
        sweep, sweep_sha = engine.read(sweep_name, ticket)
        selection, _ = engine.read(selection_name, ticket)
        require(sweep["selection"]["sha256"] == selection_digest(selection),
                f"{sweep_name} ran on another selection than {selection_name}")
        panel = chance["ancestries"][ancestry]
        require(panel["sweep"]["sha256"] == sweep_sha,
                f"the {ancestry} chance-LD entry names another sweep than the committed {sweep_name}")
        analyses = sweep["analyses"]
        tier = sum(a["rows"]["tier_rows"] for a in analyses)
        shared = sum(a["rows"]["shared_set"] for a in analyses)
        summary = counts_summary(sweep["counts"])
        best = sweep["evaluation"]["best"]
        require(not best["qualifies"], f"{ancestry} now names a qualifying per-lead point")
        nearest = point(sweep["evaluation"]["configs"], best["nearest_d_cm"], best["nearest_w_bp"])["by_r2"]
        index_chance = sweep["chance_ld"]
        rows = sweep["counts"]["configs"]
        raw_rows[ancestry] = rows
        floors = selection.get("quota_floors")
        out[ancestry] = {
            "stores": sorted({c["store"] for c in selection["chosen"]}),
            "seed": selection["rule"]["seed"],
            "pool": selection["pool_size"],
            "no_observed_rows": selection["n_analyses_without_observed_rows"],
            "drawn": len(selection["chosen"]),
            "swept": len(analyses),
            "below_floor": len(sweep["left_out_below_shared_set_floor"]["analyses"]),
            "tier_rows": tier, "shared_share": shared / tier,
            "swept_max_abs_z": max(a["selection"]["max_abs_z"] for a in analyses),
            "z_quota_in_pool": None if floors is None else floors["min_abs_z_100"]["in_pool"],
            "samples": panel["panel"]["samples"],
            "populations": sweep["genetic_map"]["populations"],
            "counts": {key: summary[key] for key in (
                "points", "c1_met", "c2_met", "both_met", "rho_min", "rho_max", "c1_holds_at_d", "named",
                "c3_holds_at_w")}
            | {"c3": [{"w_bp": c["w_bp"], "d_cm_that_hold": c["d_cm_that_hold"]} for c in summary["c3"]],
               "configs": [{key: c[key] for key in ("d_cm", "w_bp", "rho", "median_ratio")}
                           for c in summary["configs"]]},
            "nearest": {"d_cm": best["nearest_d_cm"], "w_bp": best["nearest_w_bp"],
                        "primary": rates(nearest[R2_PRIMARY]), "legacy": rates(nearest[R2_LEGACY]),
                        "splits": split_pairs(nearest[R2_PRIMARY]["spurious_split_pairs"])},
            "chance_index_pairs": {"pairs_evaluable": index_chance["pairs_evaluable"],
                                   "share_r2_ge_0.01": index_chance["share_r2_ge_0.01"],
                                   "share_r2_ge_0.001": index_chance["share_r2_ge_0.001"]},
            "chance_panel": {"pool_variants": panel["pool"]["variants"], "variants_used": panel["variants"]["used"],
                             "pairs": panel["pairs_sampled"],
                             "share_r2_ge_0.001": panel["share_r2_ge_0.001"],
                             "share_r2_ge_0.01": panel["share_r2_ge_0.01"],
                             "share_r2_ge_strong": panel[f"share_r2_ge_{STRONG_R2}"]},
            "at": {label: {"d_cm": d_out(d), "w_bp": w, "rho": point(rows, d, w)["rho"],
                           "median_ratio": point(rows, d, w)["median_ratio"],
                           "median_abs_log2": point(rows, d, w)["median_abs_log2"],
                           "lost_to_density": point(rows, d, w)["share_of_leads_lost_to_density"]}
                   for label, d, w in (("d0.1_w1mb", 0.1, 1_000_000), ("bp_w1mb", BP_ONLY, 1_000_000),
                                       ("d0.1_w10mb", 0.1, 10_000_000), ("bp_w10mb", BP_ONLY, 10_000_000))},
        }
    require(out["EUR"]["samples"] == tracer["panel"]["samples"], "the EUR panel's sample count differs")
    require(out["EUR"]["swept"] == eur["swept"], "#405's EUR column is not #403's sweep")

    # AFR's panel fit: the Analyses below the floor fall in two groups.
    afr, _ = engine.read("ancestries_afr_sweep.json", "#405")
    shares = [b["shared_set_share"] for b in afr["left_out_below_shared_set_floor"]["analyses"]]
    near_floor = [s for s in shares if s >= 0.5]
    far = [s for s in shares if s < 0.5]
    out["AFR"]["below_floor_groups"] = {"near_floor": len(near_floor), "near_floor_min": min(near_floor),
                                        "near_floor_max": max(near_floor), "far": len(far), "far_max": max(far)}

    # EAS: its named point against bp-only at the named W, on every count metric.
    named = out["EAS"]["counts"]["named"]
    named_row = point(raw_rows["EAS"], named["d_cm"], named["w_bp"])
    bp_row = point(raw_rows["EAS"], *NAMED_POINT)
    metrics = ("rho", "median_ratio", "median_abs_log2", "leads_full", "leads_hm3",
               "share_of_leads_lost_to_density")
    out["EAS"]["named_ties_bp_w10mb"] = all(named_row[m] == bp_row[m] for m in metrics)
    out["EAS"]["bp_w10mb_meets_c1_c2"] = bp_row["c1"] and bp_row["c2"]
    return out


def extract_cost(engine: Engine) -> dict[str, Any]:
    cost, _ = engine.read("cost.json", "#406")
    provenance = cost["provenance"]
    loads: list[dict[str, float]] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "loads" and isinstance(value, list):
                    loads.extend(value)
                else:
                    collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(cost)
    slices = []
    for s in cost["slices"]:
        stages, budget, ratio, rows = s["stages"], s["budget"], s["ratio"], s["rows"]
        over = over_abs_z(rows)
        slices.append({
            "name": s["name"], "store": s["store"]["store_id"], "analysis_id": s["analysis_id"],
            "threshold": s["threshold"], "tier": rows["tier"],
            "over_abs_z": over,
            # cost.py counts these over every tier row, imputed and off-map included.
            "rows_abs_z_over": rows[f"rows_abs_z_over_{over}"],
            "share_abs_z_over": rows[f"rows_abs_z_over_{over}"] / rows["tier"],
            "query_s": budget["query_s"], "resolved_query_s": budget["resolved_query_s"],
            "positions_s": stages["positions"]["median_s"], "positions_array_s": stages["positions_array"]["median_s"],
            "cm_s": stages["cm"]["median_s"], "rule_s": stages["rule"]["median_s"],
            "rule_fast_s": stages["rule_fast"]["median_s"],
            "pruning_s": budget["pruning_s"], "ratio": budget["ratio"],
            "pruning_over_resolved": budget["pruning_s"] / budget["resolved_query_s"],
            "fast_pruning_s": budget["fast_pruning_s"], "fast_ratio": budget["fast_ratio"],
            "fast_ratio_of_resolved": budget["fast_ratio_of_resolved"],
            "resolved_query_peak_mb": stages["resolved_query"]["peak_mb"],
            "resolved_query_start_mb": stages["resolved_query"]["rss_start_mb"],
            "plink2_s": ratio["clumping"]["plink2_s"], "clumping_end_to_end_s": ratio["clumping"]["end_to_end_s"],
            "ratio_plink2": ratio["ratios"]["plink2_only"], "ratio_fast_plink2": ratio["ratios_fast"]["plink2_only"],
        })
    ranges = {}
    for rule, key in (("as_written", "ratios"), ("fast", "ratios_fast")):
        for bill in ("plink2_only", "end_to_end"):
            values = [s["ratio"][key][bill] for s in cost["slices"]]
            ranges[f"{rule}_{bill}"] = {"min": min(values), "max": max(values),
                                       "cheaper_min": 1 / max(values), "cheaper_max": 1 / min(values)}
    largest = max(cost["per_variant_cm"], key=lambda p: p["variants"])
    cm_bytes = largest["variants"] * dtype_bytes(largest["cm_dtype"])
    params = cost["parameters"]
    return {
        "measured_on": provenance["measured_at"][:10],
        "commit": provenance["commit"],
        "machine": {"cpu_model": provenance["machine"]["cpu_model"], "cores": provenance["machine"]["cores"]},
        "max_load": max(entry["one"] for entry in loads),
        "parameters": {"d_cm": params["d_cm"], "w_bp": params["w_bp"], "repeats": params["repeats"],
                       "threads": params["threads"], "budget": params["budget"]["judged"]},
        # `mapped_bytes` is the chromosome, position and cM arrays together (cost.py);
        # the cM array alone is one value of `cm_dtype` per variant.
        "per_variant_cm": {"store": largest["store"]["store_id"], "variants": largest["variants"],
                           "off_map": largest["off_map_variants"],
                           "off_map_chromosomes": largest["off_map_chromosomes"],
                           "mapped_bytes": largest["mapped_bytes"], "cm_bytes": cm_bytes,
                           "position_bytes": largest["mapped_bytes"] - cm_bytes,
                           "read_s": largest["stages"]["read_variant_table"]["median_s"],
                           "interpolate_s": largest["stages"]["interpolate"]["median_s"]},
        "slices": slices,
        "ratio_ranges": ranges,
        "every_fast_rule_identical": all(s["leads"]["identical_fast_rule"] for s in cost["slices"]),
        "slices_within_budget": sum(s["budget"]["within_budget"] for s in cost["slices"]),
    }


def extract_profile(engine: Engine) -> dict[str, Any]:
    profile, _ = engine.read("eur_profile.json", "#409")
    sweep, sweep_sha = engine.read("eur_sweep.json", "#403")
    selection, _ = engine.read("eur_sweep_selection.json", "#403")
    inputs = profile["inputs"]
    require(inputs["sweep_403"]["sha256"] == sweep_sha, "#409 read another eur_sweep.json than #403 committed")
    require(inputs["selection_sha256"] == selection_digest(selection), "#409 ran on another selection")
    judgement = profile["judgement"]
    require(judgement is not None, "#409 withheld its judgement")
    counts = sweep["counts"]["configs"]
    for baseline in judgement["bp_only"]:
        mine = point(counts, BP_ONLY, baseline["w_bp"])
        for key in ("rho", "median_ratio", "median_abs_log2", "leads_full", "leads_hm3",
                    "share_of_leads_lost_to_density", "c1", "c2"):
            require(baseline[key] == mine[key], f"#409's bp-only W {baseline['w_bp']} {key} differs from #403's")
    configs = sweep["evaluation"]["configs"]
    named = judgement["named_analysis"]
    named_leads = {(b["d_cm"], b["w_bp"]): b["locus_leads"] for b in named["set_beside"]}
    baselines = []
    for beside in judgement["set_beside"]:
        mine = point(configs, beside["d_cm"], beside["w_bp"])["by_r2"][R2_PRIMARY]
        for key in ("locus_leads", "index_variants", "spurious_splits", "lost_signals"):
            require(beside["per_lead"][key] == mine[key],
                    f"#409's {beside['d_cm']}/{beside['w_bp']} per-lead {key} differs from #403's")
        require(beside["rho"] == point(counts, beside["d_cm"], beside["w_bp"])["rho"],
                "#409's set-beside rho differs from #403's")
        _, strongest = strongest_band(beside["per_lead"]["by_abs_z"])
        baselines.append({"d_cm": d_out(beside["d_cm"]), "w_bp": beside["w_bp"], "rho": beside["rho"],
                          "c1": beside["c1"], "median_ratio": beside["median_ratio"],
                          "median_abs_log2": beside["median_abs_log2"], "c2": beside["c2"]}
                         | rates(beside["per_lead"])
                         | {"strong_disagreement": band_disagreement(strongest),
                            "named_analysis_leads": named_leads[(beside["d_cm"], beside["w_bp"])]})
    beside_keys = [f"{b['d_cm']}/{b['w_bp']}" for b in judgement["set_beside"]]
    named_key, tracer_key = f"{NAMED_POINT[0]}/{NAMED_POINT[1]}", f"{TRACER_POINT[0]}/{TRACER_POINT[1]}"
    require(sorted(beside_keys) == sorted([named_key, tracer_key]), f"#409 set its rules beside {beside_keys}")
    named_rules = {r["label"]: r for r in named["rules"]}
    narrow_bin = min(profile["grid"]["bin_bp"])
    rules = []
    for rule in judgement["rules"]:
        band, strongest = strongest_band(rule["per_lead"]["by_abs_z"])
        at_10mb = next(c for c in rule["c3"] if c["w_bp"] == NAMED_POINT[1])
        rules.append({
            "label": rule["label"], "rule": rule["rule"], "narrow_bin": rule.get("bin_bp") == narrow_bin,
            "rho": rule["rho"], "c1": rule["c1"], "median_ratio": rule["median_ratio"],
            "median_abs_log2": rule["median_abs_log2"], "c2": rule["c2"],
            "lost_to_density": rule["share_of_leads_lost_to_density"],
            "c3_holds_at_w": [c["w_bp"] for c in rule["c3"] if c["c3"]],
            "delta_rho_w10mb": at_10mb["delta_rho"], "delta_abs_log2_w10mb": at_10mb["delta_abs_log2"],
        } | rates(rule["per_lead"]) | {
            "strong_from": band_floor(band), "strong_disagreement": band_disagreement(strongest),
            "beats_named_at_strong_peaks": rule["beats_strong_peaks"][named_key],
            "beats_tracer_at_strong_peaks": rule["beats_strong_peaks"][tracer_key],
            "named_analysis_leads": named_rules[rule["label"]]["locus_leads"],
        })
    narrow = [r for r in rules if r["narrow_bin"]]
    outside = [next(r for r in a["rules"] if r["rule"] == "ld_blocks")["outside"] for a in profile["analyses"]]
    valleys = profile["valley_cells"]["summary"]
    return {
        "analyses": judgement["analyses"],
        "profile_tier": inputs["profile_tier"],
        "bins_bp": profile["grid"]["bin_bp"],
        "thresholds": profile["grid"]["thresholds"],
        "blocks": inputs["blocks"]["blocks"],
        "blocks_rows_outside": sum(o["rows"] for o in outside),
        "blocks_stretches_outside": sum(o["stretches"] for o in outside),
        "rules": rules,
        "baselines": baselines,
        "named_analysis": {"analysis_id": named["analysis_id"], "index_variants": named["index_variants"]},
        "narrow_bin_ratio_min": min(r["median_ratio"] for r in narrow),
        "narrow_bin_ratio_max": max(r["median_ratio"] for r in narrow),
        "narrow_bin_rules": len(narrow),
        "narrow_bin_c2_met": sum(r["c2"] for r in narrow),
        "c3_anywhere": [{"label": r["label"], "w_bp": w} for r in rules for w in r["c3_holds_at_w"]],
        "valleys": {"no_row_valleys": profile["valley_cells"]["no_row_valleys"],
                    "by_bin": [{"bin_bp": int(b), "sampled": v["valleys"], "share_gap": v["share_gap"],
                                "stop_rule_trips": v["stop_rule_trips"]}
                               for b, v in valleys.items() if b != "pooled"]},
    }


def build(engine: Engine) -> dict[str, Any]:
    method = extract_method(engine)
    tracer = extract_tracer(engine)
    eur = extract_eur(engine)
    maps = extract_maps(engine)
    ancestries = extract_ancestries(engine, eur)
    cost = extract_cost(engine)
    profile = extract_profile(engine)
    return {
        "about": ("Values the Locus Lead prototype report shows, read from the engine's committed outputs "
                  "by tools/locus_leads_prototype_extract.py (opengwas/opengwasdb-stores#444)."),
        "engine": {"repository": "opengwas/opengwasdb", "commit": engine.commit},
        "sources": engine.sources,
        "method": method,
        "tracer": tracer,
        "eur": eur,
        "maps": maps,
        "ancestries": ancestries,
        "cost": cost,
        "profile": profile,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", type=Path, required=True, help="an opengwasdb checkout holding --commit")
    parser.add_argument("--commit", required=True, help="the engine commit whose outputs are read")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    extract = build(Engine(args.engine, args.commit))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(extract, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: {len(extract['sources'])} sources at {extract['engine']['commit']}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
