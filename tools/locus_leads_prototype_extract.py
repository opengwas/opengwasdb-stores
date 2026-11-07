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

Where a later ticket's output copies #403's numbers -- #404's pyrho-mean record
and #409's baselines, rows, criteria and per-Analysis records -- every field the
copy shares with #403's output must be equal, at any depth; so must the grid and
criteria #405's sweeps share with #403's, the sweep and selection digests each
later ticket names, and the panel sizes. Otherwise the extract refuses, naming
the field and both values, and writes nothing. A key an output lacks refuses the
same way.

The per-Analysis counts the report plots come from #403's sweep (bp-only, cM)
and #409's profile (prominence, LD blocks). Both must give each Analysis the
same index variants, and each rule's rho, median ratio and median |log2|,
recomputed from those counts, must equal the summary its output committed.

The one input that is not an engine output is the chromosome lengths behind
the bp-only rule's ceiling. They are read from the `##contig` headers of the
EUR panel's variant tables (`chr<c>.pvar.zst`, at the path #403's sweep names),
decompressed with the `zstd` the dev environment ships, and each file's sha256
is recorded beside its length.

The region figures read #409's `eur_regions.json`. The extract refuses it unless
the sweep, profile, selection, map, HapMap3 list, LD blocks and panel tables it
records reading are the ones read here, and unless each Analysis's whole-genome
index variants match the per-Analysis records and no method has more leads in
its view than in the whole genome. The regions' rows go to a small TSV beside
the extract, whose sha256 the extract records.

Run from the repository root:

    pixi run -e dev python3 tools/locus_leads_prototype_extract.py \\
        --engine <an opengwasdb checkout holding the commit> --commit 3c2bdd9 \\
        --output docs/data/locus-leads-prototype.json \\
        --regions-rows docs/data/locus-leads-regions.tsv
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
# The Top-Hit tier every sweep reads, and #406 fixed its budget on.
TIER = 5e-08
MAPS = ("pyrho-mean", "pyrho-median", "hapmap2")
ANCESTRIES = ("EUR", "AFR", "EAS", "AMR")
NEAR_BAND = "<=1Mb"
# A disagreeing pair this strongly linked is far above what chance gives at the panels' sizes.
STRONG_R2 = "0.05"
# The Analysis #403's decision and #409 single out: the strongest peak's shadows.
NAMED_ANALYSIS = "GCST90162563"
# The prominence rule the owner's decision names; the per-Analysis figure and the alternatives table show it.
PROMINENCE = "B 50 kb, f 0.75"
# How close a statistic recomputed from the per-Analysis counts must come to the committed one.
SAME_TO = 1e-12
# The regions file's names for the four rules, and the per-Analysis counts' names for the same ones.
REGION_METHODS = {"bp_only": "bp_only", "named_cm": "cm", "prominence": "prominence", "ld_blocks": "ld_blocks"}


class Refused(SystemExit):
    """Two outputs disagree, or one is not what the report assumes."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Refused(f"refused: {message}")


def require_same(copy: Any, original: Any, where: str, own: tuple[str, ...] = ()) -> None:
    """Every field `copy` shares with `original` must be equal, at any depth.

    Dicts are compared on the keys both hold (less `own`, the keys at the top
    that are the copy's own and not copied), lists element by element and of
    equal length, anything else by value. A difference refuses, naming the path
    and both values. A top level that shares no key is refused too, because
    then nothing was checked.
    """
    if isinstance(copy, dict) and isinstance(original, dict):
        require(bool((copy.keys() & original.keys()) - set(own)), f"{where} shares no field with #403's output")

    def walk(a: Any, b: Any, path: str, skip: tuple[str, ...]) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted((a.keys() & b.keys()) - set(skip)):
                walk(a[key], b[key], f"{path}.{key}", ())
        elif isinstance(a, list) and isinstance(b, list):
            require(len(a) == len(b), f"{where}{path}: {len(a)} entries in the copy, {len(b)} in the original")
            for index, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{index}]", ())
        else:
            require(a == b, f"{where}{path}: {a!r} in the copy, {b!r} in the original")

    walk(copy, original, "", own)


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
            try:
                raw = self._git("show", f"{self.commit}:{path}")
            except subprocess.CalledProcessError as missing:
                raise Refused(f"refused: {path} is not in the engine at {self.commit}; nothing written") from missing
            digest = hashlib.sha256(raw).hexdigest()
            self.sources.append({"path": path, "ticket": ticket, "bytes": len(raw), "sha256": digest})
            self._read[name] = (json.loads(raw), digest)
        return self._read[name]

    def committed_on(self, name: str) -> str:
        """The date of the last commit that changed an output, as of this commit."""
        return self._git("log", "-1", "--format=%cs", self.commit, "--",
                         f"{OUTPUTS}/{name}").decode().strip()


class Panel:
    """A genotype panel's per-chromosome variant tables on disk, each read whole and recorded."""

    def _bytes(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def _decompress(self, raw: bytes) -> bytes:
        return subprocess.run(["zstd", "-dc"], input=raw, check=True, capture_output=True).stdout

    def header(self, path: str) -> tuple[str, dict[str, Any]]:
        """A variant table's header lines (up to `#CHROM`), and its path, size and sha256."""
        raw = self._bytes(path)
        header = self._decompress(raw).split(b"\n#CHROM", 1)[0].decode()
        return header, {"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


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


def average_ranks(values: list[float]) -> list[float]:
    """1-based ranks, ties given the mean of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        for i in order[start:end + 1]:
            ranks[i] = (start + end) / 2 + 1
        start = end + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float:
    """Spearman's rho, as #403's `counts.py` computes it: the Pearson correlation of average ranks."""
    a, b = average_ranks(x), average_ranks(y)
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    cross = sum((p - mean_a) * (q - mean_b) for p, q in zip(a, b))
    return cross / math.sqrt(sum((p - mean_a) ** 2 for p in a) * sum((q - mean_b) ** 2 for q in b))


def split_pairs(pairs: dict[str, Any]) -> dict[str, Any]:
    """How far a set of disagreeing pairs reach, and how many sit well above chance LD."""
    bands = pairs["by_distance_band"]
    n = pairs["n"]
    beyond = sum(counts["n"] for band, counts in bands.items() if band != NEAR_BAND)
    strong = sum(counts[f"r2_ge_{STRONG_R2}"] for counts in bands.values())
    return {"n": n, "near_band_bp": distance_band_bp(NEAR_BAND),
            "beyond_1mb": beyond, "beyond_1mb_share": beyond / n if n else None,
            "strong_r2": float(STRONG_R2), "r2_ge_strong_share": strong / n if n else None}


# --------------------------------------------------------------------------- copies of #403


def eur_inputs(engine: Engine) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """#403's sweep, its sha256 and its selection, the sweep checked to have run on that selection."""
    sweep, sweep_sha = engine.read("eur_sweep.json", "#403")
    selection, _ = engine.read("eur_sweep_selection.json", "#403")
    require(sweep["selection"]["sha256"] == selection_digest(selection),
            "eur_sweep.json ran on another selection than eur_sweep_selection.json")
    return sweep, sweep_sha, selection


def sweep_analysis(sweep: dict[str, Any], store: str, analysis_id: str) -> dict[str, Any]:
    found = [a for a in sweep["analyses"] if a["store"] == store and a["analysis"]["analysis_id"] == analysis_id]
    require(len(found) == 1, f"#403's sweep holds {len(found)} records of {store}/{analysis_id}")
    return found[0]


def index_variants(record: dict[str, Any]) -> int:
    """An Analysis's r2 0.01 index variants: clumping does not depend on D or W, so every point agrees."""
    counts = {config["by_r2"][R2_PRIMARY]["index_variants"] for config in record["configs"]}
    require(len(counts) == 1, f"#403's {record['analysis']['analysis_id']} has index-variant counts {counts}")
    return counts.pop()


def check_maps_copies(maps: dict[str, Any], sweep: dict[str, Any], sweep_sha: str,
                      selection: dict[str, Any]) -> None:
    """#404's record of the pyrho mean is #403's sweep; every map keeps #403's grid and criteria."""
    require(maps["selection"]["sha256"] == selection_digest(selection), "#404 ran on another selection")
    mean = maps["maps"]["pyrho-mean"]
    require(mean["sweep_sha256"] == sweep_sha, "#404's pyrho-mean sweep is not #403's committed eur_sweep.json")
    require(maps["shared_sets_equal"], "#404's maps saw different shared sets")
    for name, record in maps["maps"].items():
        require_same(record["grid"], sweep["grid"], f"#404 {name}.grid")
        require_same(record["criteria"], sweep["counts"]["criteria"], f"#404 {name}.criteria")
    for key in ("genetic_map", "spikes", "counts"):
        require_same(mean[key], sweep[key], f"#404 pyrho-mean.{key}")
    require_same(mean["best_by_per_lead"], sweep["evaluation"]["best"], "#404 pyrho-mean.best_by_per_lead")
    require_same(mean["per_lead"], sweep["evaluation"]["configs"], "#404 pyrho-mean.per_lead")
    require(mean["analyses"] == len(sweep["analyses"]),
            f"#404 pyrho-mean.analyses: {mean['analyses']} in the copy, {len(sweep['analyses'])} in the original")
    below = sweep["left_out_below_shared_set_floor"]
    require_same(mean["below_shared_set_floor"],
                 {"floor": below["floor"],
                  "analyses": [f"{b['store']}/{b['analysis']['analysis_id']}" for b in below["analyses"]]},
                 "#404 pyrho-mean.below_shared_set_floor")


def check_profile_copies(profile: dict[str, Any], sweep: dict[str, Any], sweep_sha: str,
                         selection: dict[str, Any]) -> None:
    """#409's baselines, rows, criteria and per-Analysis records are #403's, field for field."""
    inputs = profile["inputs"]
    require(inputs["sweep_403"]["sha256"] == sweep_sha, "#409 read another eur_sweep.json than #403 committed")
    require(inputs["selection_sha256"] == selection_digest(selection), "#409 ran on another selection")
    judgement = profile["judgement"]
    require(judgement is not None, "#409 withheld its judgement")
    for key in ("genetic_map", "panel", "hm3"):
        require_same(inputs[key], sweep[key], f"#409 inputs.{key}")
    counts = sweep["counts"]
    # #409 states its own reading of C3 (a rule has no W of its own); the rest is #403's wording.
    require_same(judgement["criteria"], counts["criteria"], "#409 judgement.criteria", own=("c3_reading",))
    require_same(judgement["rows"], counts["rows"], "#409 judgement.rows")
    require(judgement["analyses"] == counts["analyses"],
            f"#409 judgement.analyses: {judgement['analyses']} in the copy, {counts['analyses']} in the original")
    for index, baseline in enumerate(judgement["bp_only"]):
        require_same(baseline, point(counts["configs"], BP_ONLY, baseline["w_bp"]),
                     f"#409 judgement.bp_only[{index}]")
    configs = sweep["evaluation"]["configs"]
    for index, beside in enumerate(judgement["set_beside"]):
        d, w = beside["d_cm"], beside["w_bp"]
        where = f"#409 judgement.set_beside[{index}]"
        require_same(beside, point(counts["configs"], d, w), where, own=("per_lead", "leads_per_index_variant"))
        require_same(beside["per_lead"], point(configs, d, w)["by_r2"][R2_PRIMARY], f"{where}.per_lead")
        require_same(beside["leads_per_index_variant"], point(counts["leads_per_index_variant"], d, w)["z"],
                     f"{where}.leads_per_index_variant")
    for index, rule in enumerate(judgement["rules"]):
        require(rule["rows_median_abs_log2"] == counts["rows"]["median_abs_log2"],
                f"#409 judgement.rules[{index}].rows_median_abs_log2: {rule['rows_median_abs_log2']!r} in the "
                f"copy, {counts['rows']['median_abs_log2']!r} in the original")
    named = judgement["named_analysis"]
    record = next(a for a in sweep["analyses"] if a["analysis"]["analysis_id"] == named["analysis_id"])
    require_same({"max_abs_z": named["max_abs_z"], "index_variants": named["index_variants"]},
                 {"max_abs_z": record["selection"]["max_abs_z"], "index_variants": index_variants(record)},
                 f"#409 judgement.named_analysis ({named['analysis_id']})")
    for index, beside in enumerate(named["set_beside"]):
        leads = point(record["configs"], beside["d_cm"], beside["w_bp"])["by_r2"][R2_PRIMARY]["locus_leads"]
        require(beside["locus_leads"] == leads, f"#409 judgement.named_analysis.set_beside[{index}].locus_leads: "
                                                f"{beside['locus_leads']!r} in the copy, {leads!r} in the original")
    for index, analysis in enumerate(profile["analyses"]):
        mine = sweep_analysis(sweep, analysis["store"], analysis["analysis"]["analysis_id"])
        where = f"#409 analyses[{index}] ({analysis['analysis']['analysis_id']})"
        # `query` there is #409's read of the 5e-4 profile tier, not #403's 5e-8 read.
        require_same(analysis, mine, where, own=("query",))
        require(analysis["index_variants"] == index_variants(mine),
                f"{where}.index_variants: {analysis['index_variants']!r} in the copy, "
                f"{index_variants(mine)!r} in the original")
    require_same(profile["not_swept_by_403"],
                 [{"store": b["store"], "analysis_id": b["analysis"]["analysis_id"]}
                  for b in sweep["left_out_below_shared_set_floor"]["analyses"]],
                 "#409 not_swept_by_403")


def check_same_conventions(sweep: dict[str, Any], selection: dict[str, Any], eur: dict[str, Any],
                           eur_selection: dict[str, Any], where: str) -> None:
    """Another ancestry's sweep keeps #403's grid, criteria, HapMap3 list and row cap."""
    require_same(sweep["grid"], eur["grid"], f"{where}.grid")
    require_same(sweep["counts"]["criteria"], eur["counts"]["criteria"], f"{where}.counts.criteria")
    require_same(sweep["evaluation"]["criteria"], eur["evaluation"]["criteria"], f"{where}.evaluation.criteria")
    require_same(sweep["hm3"], eur["hm3"], f"{where}.hm3")
    require(selection["rule"]["max_observed_rows"] == eur_selection["rule"]["max_observed_rows"],
            f"{where}: a row cap of {selection['rule']['max_observed_rows']!r}, "
            f"not #403's {eur_selection['rule']['max_observed_rows']!r}")


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
    sweep, _ = engine.read("eur_sweep.json", "#403")
    # The tracer ran on the same EUR map and panel the sweep names.
    require_same(tracer["genetic_map"], sweep["genetic_map"], "#402 genetic_map")
    require_same(tracer["panel"], sweep["panel"], "#402 panel")
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
    sweep, _, selection = eur_inputs(engine)
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
            "within_limits": sum(1 for p in points if p["spurious_split_rate"] <= criteria["spurious_split_rate_max"]
                                 and p["lost_signal_rate"] <= criteria["lost_signal_rate_max"]),
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
        "spike_count": len(sweep["spikes"]),
        "spikes": [{"population": s["population"], "chromosome": s["chromosome"], "position": s["position"],
                    "interval_bp": s["interval_bp"], "population_cm": s["population_cm"],
                    "mean_map_cm": s["mean_map_cm"]} for s in sweep["spikes"]],
        "counts": counts_summary(sweep["counts"]),
    }


def extract_maps(engine: Engine) -> dict[str, Any]:
    maps, _ = engine.read("eur_maps.json", "#404")
    sweep, sweep_sha, selection = eur_inputs(engine)
    check_maps_copies(maps, sweep, sweep_sha, selection)
    mean = maps["maps"]["pyrho-mean"]
    out = []
    for name in MAPS:
        record = maps["maps"][name]
        summary = counts_summary(record["counts"])
        at_tracer = point(record["per_lead"], *TRACER_POINT)["by_r2"][R2_PRIMARY]
        dropped = record["genetic_map"].get("dropped_to_a_non_decreasing_cM")
        best = record["best_by_per_lead"]
        best_point = (best["d_cm"], best["w_bp"]) if best["qualifies"] else (best["nearest_d_cm"], best["nearest_w_bp"])
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
            "best_per_lead": {"qualifies": record["best_by_per_lead"]["qualifies"],
                              "d_cm": best_point[0], "w_bp": best_point[1]}
            | rates(point(record["per_lead"], *best_point)["by_r2"][R2_PRIMARY]),
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
    eur_sweep, _, eur_selection = eur_inputs(engine)
    out = {}
    raw_rows = {}
    for ancestry in ANCESTRIES:
        if ancestry == "EUR":
            sweep_name = "eur_sweep.json"
            sweep, sweep_sha, selection = eur_inputs(engine)
        else:
            stem = f"ancestries_{ancestry.lower()}"
            sweep_name, selection_name = f"{stem}_sweep.json", f"{stem}_selection.json"
            sweep, sweep_sha = engine.read(sweep_name, "#405")
            selection, _ = engine.read(selection_name, "#405")
            require(sweep["selection"]["sha256"] == selection_digest(selection),
                    f"{sweep_name} ran on another selection than {selection_name}")
            check_same_conventions(sweep, selection, eur_sweep, eur_selection, f"#405 {ancestry}")
        panel = chance["ancestries"][ancestry]
        require(panel["sweep"]["sha256"] == sweep_sha,
                f"the {ancestry} chance-LD entry names another sweep than the committed {sweep_name}")
        require_same(panel["panel"], sweep["panel"], f"#405 chance-LD {ancestry}.panel")
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


def require_ratio(ratio: float, numerator: float, denominator: float, where: str) -> None:
    """A ratio #406 publishes must follow from the two times it publishes beside it (all rounded)."""
    require(math.isclose(ratio, numerator / denominator, rel_tol=1e-3, abs_tol=1e-4),
            f"{where}: {ratio!r} published, {numerator!r} / {denominator!r} = {numerator / denominator!r}")


def extract_cost(engine: Engine) -> dict[str, Any]:
    cost, _ = engine.read("cost.json", "#406")
    _, _, selection = eur_inputs(engine)
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
        where = f"#406 {s['name']}"
        # Two workloads: the full tier a query returns (budget) and the shared set both methods see (ratio).
        require_ratio(budget["ratio"], budget["pruning_s"], budget["query_s"], f"{where} budget.ratio")
        require_ratio(budget["fast_ratio"], budget["fast_pruning_s"], budget["query_s"], f"{where} budget.fast_ratio")
        require_ratio(budget["fast_ratio_of_resolved"], budget["fast_pruning_s"], budget["resolved_query_s"],
                      f"{where} budget.fast_ratio_of_resolved")
        for rule, key, seconds in (("as written", "ratios", "pruning_s"), ("block", "ratios_fast", "fast_pruning_s")):
            for bill, clump in (("plink2_only", "plink2_s"), ("end_to_end", "end_to_end_s")):
                require_ratio(ratio[key][bill], ratio[seconds], ratio["clumping"][clump],
                              f"{where} ratio.{key}.{bill} ({rule})")
        slices.append({
            "name": s["name"], "store": s["store"]["store_id"], "analysis_id": s["analysis_id"],
            "threshold": s["threshold"], "tier": rows["tier"],
            "over_abs_z": over,
            # cost.py counts these over every tier row, imputed and off-map included.
            "rows_abs_z_over": rows[f"rows_abs_z_over_{over}"],
            "share_abs_z_over": rows[f"rows_abs_z_over_{over}"] / rows["tier"],
            "query_s": budget["query_s"], "resolved_query_s": budget["resolved_query_s"],
            "positions_s": stages["positions"]["median_s"], "positions_array_s": stages["positions_array"]["median_s"],
            "pruning_s": budget["pruning_s"], "ratio": budget["ratio"],
            "fast_pruning_s": budget["fast_pruning_s"], "fast_ratio": budget["fast_ratio"],
            "fast_ratio_of_resolved": budget["fast_ratio_of_resolved"],
            "resolved_query_peak_mb": stages["resolved_query"]["peak_mb"],
            "resolved_query_start_mb": stages["resolved_query"]["rss_start_mb"],
            "shared_rows": ratio["rows"], "shared_pruning_s": ratio["pruning_s"],
            "shared_fast_pruning_s": ratio["fast_pruning_s"],
            # Clumping end to end: building the shared set (matching the rows to the panel), ranking it, plink2.
            "shared_set_s": s["clumping"]["stages"]["shared_set"]["median_s"],
            "plink2_s": ratio["clumping"]["plink2_s"], "clumping_end_to_end_s": ratio["clumping"]["end_to_end_s"],
            "ratio_plink2": ratio["ratios"]["plink2_only"], "ratio_fast_plink2": ratio["ratios_fast"]["plink2_only"],
            "ratio_fast_end_to_end": ratio["ratios_fast"]["end_to_end"],
        })
    ranges = {}
    for rule, key in (("as_written", "ratios"), ("fast", "ratios_fast")):
        for bill in ("plink2_only", "end_to_end"):
            values = [s["ratio"][key][bill] for s in cost["slices"]]
            ranges[f"{rule}_{bill}"] = {"min": min(values), "max": max(values),
                                       "cheaper_min": 1 / max(values), "cheaper_max": 1 / min(values)}
    # The timed Analyses #403's draw excluded for their row count, by their 5e-8 tier.
    excluded = {e["analysis_id"] for e in selection["excluded_over_max_rows"]}
    artefacts = [{"analysis_id": s["analysis_id"], "store": s["store"], "tier": s["tier"],
                  "over_abs_z": s["over_abs_z"], "rows_abs_z_over": s["rows_abs_z_over"],
                  "share_abs_z_over": s["share_abs_z_over"]}
                 for s in slices if s["analysis_id"] in excluded and s["threshold"] == TIER]
    require(sorted(a["analysis_id"] for a in artefacts) == sorted(excluded),
            f"#406 timed {sorted(a['analysis_id'] for a in artefacts)} of #403's excluded {sorted(excluded)}")
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
                           "read_s": largest["stages"]["read_variant_table"]["median_s"],
                           "interpolate_s": largest["stages"]["interpolate"]["median_s"]},
        "slices": slices,
        "largest_by_rows": max(slices, key=lambda s: s["tier"])["name"],
        "artefact_analyses": artefacts,
        "ratio_ranges": ranges,
        "every_fast_rule_identical": all(s["leads"]["identical_fast_rule"] for s in cost["slices"]),
        "slices_within_budget": sum(s["budget"]["within_budget"] for s in cost["slices"]),
    }


def extract_profile(engine: Engine) -> dict[str, Any]:
    profile, _ = engine.read("eur_profile.json", "#409")
    sweep, sweep_sha, selection = eur_inputs(engine)
    check_profile_copies(profile, sweep, sweep_sha, selection)
    inputs = profile["inputs"]
    judgement = profile["judgement"]
    named = judgement["named_analysis"]
    named_leads = {(b["d_cm"], b["w_bp"]): b["locus_leads"] for b in named["set_beside"]}
    baselines = []
    for beside in judgement["set_beside"]:
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
    # #403's per-lead limits, which no rule met there: how near #409's rules come, by the larger rate.
    limits = sweep["evaluation"]["criteria"]
    within = [r for r in rules if r["spurious_split_rate"] <= limits["spurious_split_rate_max"]
              and r["lost_signal_rate"] <= limits["lost_signal_rate_max"]]
    nearest = min(rules, key=lambda r: max(r["spurious_split_rate"], r["lost_signal_rate"]))
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
        "per_lead_within_limits": len(within),
        "per_lead_nearest": nearest["label"],
        "c3_anywhere": [{"label": rule["label"], "w_bp": c["w_bp"], "delta_rho": c["delta_rho"],
                         "delta_abs_log2": c["delta_abs_log2"]}
                        for rule in judgement["rules"] for c in rule["c3"] if c["c3"]],
        "valleys": {"no_row_valleys": profile["valley_cells"]["no_row_valleys"],
                    "by_bin": [{"bin_bp": int(b), "sampled": v["valleys"], "share_gap": v["share_gap"],
                                "stop_rule_trips": v["stop_rule_trips"]}
                               for b, v in valleys.items() if b != "pooled"]},
    }


# --------------------------------------------------------------------------- per-Analysis counts


def bp_only_ceiling(sweep: dict[str, Any], panel: Panel, w_bp: int) -> dict[str, Any]:
    """The most bp-only Locus Leads at W that the shared set's chromosomes can hold.

    The rule merges a row into a lead within W bp (distance at most W), so one
    chromosome's leads lie at least W + 1 bp apart, and a chromosome of L bp
    holds at most floor((L - 1) / (W + 1)) + 1. The shared set keeps only rows
    with a finite cM, so its chromosomes are the map's. Each length is read from
    the `##contig` header of that chromosome's table in the panel #403's sweep
    names, which must be on the panel's assembly.
    """
    root, assembly = sweep["panel"]["path"], sweep["panel"]["assembly"]
    contigs = []
    for chromosome in sweep["genetic_map"]["points_per_chromosome"]:
        header, record = panel.header(f"{root}/chr{chromosome}.pvar.zst")
        where = record["path"]
        found = re.findall(r"^##contig=<ID=([^,>]+),length=(\d+),assembly=([^,>]+)>$", header, re.MULTILINE)
        require(len(found) == 1, f"{where} has {len(found)} ##contig lines with a length and an assembly, not one")
        name, length, on = found[0]
        require(name == f"chr{chromosome}", f"{where} describes {name}, not chr{chromosome}")
        require(on == assembly, f"{where} is on {on}, not on the panel's {assembly}")
        contigs.append({"chromosome": chromosome, "length": int(length)} | record)
    return {"w_bp": w_bp, "panel": root, "assembly": assembly, "contigs": contigs,
            "leads": sum((c["length"] - 1) // (w_bp + 1) + 1 for c in contigs)}


def per_analysis_counts(sweep: dict[str, Any], profile: dict[str, Any], ceiling: dict[str, Any]) -> dict[str, Any]:
    """Each EUR Analysis's index variants and its Locus Leads under the four rules the figure sets side by side.

    bp-only at #403's named point and #403's best cM point come from the
    sweep's per-Analysis records; prominence and LD blocks from the profile's.
    Every record must give an Analysis the index variants #403's sweep gives
    it, every count must be positive (the figure's axes are log), and each
    rule's rho, median ratio and median |log2|, recomputed from these counts,
    must equal the summary its output committed.
    """
    counts = sweep["counts"]
    named, named_cm = counts["named"], counts["named_cm"]
    require((named["d_cm"], named["w_bp"]) == NAMED_POINT,
            f"#403 names D {named['d_cm']}, W {named['w_bp']}, not {NAMED_POINT[0]} W {NAMED_POINT[1]}")
    judged = {rule["label"]: rule for rule in profile["judgement"]["rules"]}
    blocks = [rule["label"] for rule in profile["judgement"]["rules"] if rule["rule"] == "ld_blocks"]
    require(len(blocks) == 1, f"#409 judged {len(blocks)} LD-block rules, not one")
    from_sweep = {"bp_only": NAMED_POINT, "cm": (named_cm["d_cm"], named_cm["w_bp"])}
    from_profile = {"prominence": PROMINENCE, "ld_blocks": blocks[0]}
    profiled = {(a["store"], a["analysis"]["analysis_id"]): a for a in profile["analyses"]}
    swept = {(a["store"], a["analysis"]["analysis_id"]) for a in sweep["analyses"]}
    require(set(profiled) == swept, f"#409 and #403 hold different Analyses: {sorted(swept ^ set(profiled))}")

    rows = []
    for analysis in sweep["analyses"]:
        analysis_id = analysis["analysis"]["analysis_id"]
        profiled_analysis = profiled[(analysis["store"], analysis_id)]
        records = {key: point(analysis["configs"], *at) for key, at in from_sweep.items()}
        for key, label in from_profile.items():
            found = [rule for rule in profiled_analysis["rules"] if rule["label"] == label]
            require(len(found) == 1, f"#409 holds {len(found)} records of {label} for {analysis_id}")
            records[key] = found[0]
        index = index_variants(analysis)
        require(profiled_analysis["index_variants"] == index,
                f"{analysis_id}: #409 gives {profiled_analysis['index_variants']!r} index variants, #403 {index!r}")
        for key, record in records.items():
            theirs = record["by_r2"][R2_PRIMARY]["index_variants"]
            require(theirs == index, f"{analysis_id}: the {key} record gives {theirs!r} index variants at "
                                     f"r² {R2_PRIMARY}, #403's sweep {index!r}")
        row = {"analysis_id": analysis_id, "store": analysis["store"], "z_band": analysis["selection"]["z_band"],
               "index_variants": index,
               "leads_full": {key: record["density"]["leads_full"] for key, record in records.items()},
               "leads_hm3": {key: record["density"]["leads_hm3"] for key, record in records.items()}}
        for key in records:
            for value, what in ((row["leads_full"][key], "full"), (row["leads_hm3"][key], "HapMap3")):
                require(value > 0, f"{analysis_id}: {value!r} {key} leads on the {what} rows, which a log axis "
                                   "cannot show")
        rows.append(row)
    require(len(rows) == counts["analyses"], f"{len(rows)} Analyses, where #403 counted {counts['analyses']}")

    methods = []
    for key in (*from_sweep, *from_profile):
        committed = point(counts["configs"], *from_sweep[key]) if key in from_sweep else judged[from_profile[key]]
        full = [row["leads_full"][key] for row in rows]
        ratio = [row["leads_hm3"][key] / row["leads_full"][key] for row in rows]
        mine = {"rho": spearman(full, [row["index_variants"] for row in rows]),
                "median_ratio": statistics.median(ratio),
                "median_abs_log2": statistics.median(abs(math.log2(r)) for r in ratio)}
        for stat, value in mine.items():
            require(math.isclose(value, committed[stat], rel_tol=SAME_TO, abs_tol=SAME_TO),
                    f"{key} {stat}: {value!r} from the per-Analysis counts, {committed[stat]!r} committed")
        at = from_sweep.get(key, (None, None))
        methods.append({"key": key, "source": "eur_sweep.json" if key in from_sweep else "eur_profile.json",
                        "d_cm": d_out(at[0]), "w_bp": at[1], "label": from_profile.get(key)}
                       | {stat: committed[stat] for stat in (*mine, "c1", "c2")})

    bp_only = [row["leads_full"]["bp_only"] for row in rows]
    require(max(bp_only) <= ceiling["leads"],
            f"a bp-only count of {max(bp_only)} is above the ceiling of {ceiling['leads']} the lengths allow")
    above = [row for row in rows if row["index_variants"] > ceiling["leads"]]
    strongest = {}
    for analysis in sweep["analyses"]:
        band = analysis["selection"]["z_band"]
        strongest[band] = min(strongest.get(band, math.inf), analysis["selection"]["max_abs_z"])
    return {
        "methods": methods,
        "z_bands": sorted(strongest, key=strongest.get),
        "analyses": rows,
        "most_clumps": max(rows, key=lambda row: row["index_variants"])["analysis_id"],
        "above_ceiling": {"analyses": len(above),
                          "index_min": min(row["index_variants"] for row in above),
                          "index_max": max(row["index_variants"] for row in above),
                          "bp_only_min": min(row["leads_full"]["bp_only"] for row in above),
                          "bp_only_max": max(row["leads_full"]["bp_only"] for row in above)},
    }


def extract_counts(engine: Engine, panel: Panel) -> dict[str, Any]:
    sweep, _, _ = eur_inputs(engine)
    profile, _ = engine.read("eur_profile.json", "#409")
    ceiling = bp_only_ceiling(sweep, panel, NAMED_POINT[1])
    return per_analysis_counts(sweep, profile, ceiling) | {"ceiling": ceiling}


# --------------------------------------------------------------------------- the region figures


def regions_record(regions: dict[str, Any], *, sweep: dict[str, Any], sweep_sha: str, profile: dict[str, Any],
                   profile_sha: str, selection: dict[str, Any], counts: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """What the region figures and their prose show, and the TSV of the rows they draw.

    The regions file must have read the committed sweep, profile and selection,
    #403's map and HapMap3 list, #409's LD blocks, and the panel tables the
    ceiling read. Each Analysis's whole-genome index variants must match the
    per-Analysis records, and no rule may have more leads in its view than in
    the whole genome.
    """
    inputs = regions["inputs"]
    for name, theirs, ours in (("eur_sweep.json", inputs["sweep"]["sha256"], sweep_sha),
                               ("eur_profile.json", inputs["profile"]["sha256"], profile_sha),
                               ("eur_sweep_selection.json", inputs["selection_sha256"], selection_digest(selection))):
        require(theirs == ours, f"eur_regions.json read {name} with sha256 {theirs}, not the committed {ours}")
    require_same(inputs["genetic_map"], sweep["genetic_map"], "eur_regions.json inputs.genetic_map")
    require_same(inputs["hm3"], sweep["hm3"], "eur_regions.json inputs.hm3")
    require_same(inputs["blocks"], profile["inputs"]["blocks"], "eur_regions.json inputs.blocks")
    panel = inputs["panel"]
    require_same({key: panel[key] for key in ("path", "assembly")}, sweep["panel"], "eur_regions.json inputs.panel")
    for contig in counts["ceiling"]["contigs"]:
        theirs = panel["sha256"][contig["chromosome"]][".pvar.zst"]
        require(theirs == contig["sha256"], f"eur_regions.json read chr{contig['chromosome']}.pvar.zst with sha256 "
                                            f"{theirs}, the ceiling read {contig['sha256']}")
    sources = {m["key"]: m["source"] for m in regions["methods"]}
    ours = {theirs: next(m["source"] for m in counts["methods"] if m["key"] == mine)
            for theirs, mine in REGION_METHODS.items()}
    require(sources == ours, f"eur_regions.json draws its rules from {sources}, the per-Analysis counts from {ours}")

    per_analysis = {(a["store"], a["analysis_id"]): a for a in counts["analyses"]}
    named = regions["selection"]["named"]
    drawn = {(d["chosen"]["store"], d["chosen"]["analysis_id"]): d for d in regions["selection"]["draws"]}
    views, lines = [], ["analysis_id\tposition\tabs_z\tclump\tindex_variant"]
    for region in regions["regions"]:
        analysis = region["analysis"]
        key = (analysis["store"], analysis["analysis_id"])
        where = f"eur_regions.json {analysis['analysis_id']}"
        require(key in per_analysis, f"{where} is not one of #403's swept Analyses")
        mine = per_analysis[key]
        for theirs, field in ((analysis["index_variants"], "analysis.index_variants"),
                              (len(region["genome_index_variants"]["position"]), "genome_index_variants")):
            require(theirs == mine["index_variants"], f"{where}: {theirs!r} index variants in {field}, "
                                                      f"{mine['index_variants']!r} in the per-Analysis records")
        leads = {}
        for theirs_key, my_key in REGION_METHODS.items():
            record = region["leads"][theirs_key]
            kinds = [lead["kind"] for lead in record["leads"]]
            require(len(kinds) <= mine["leads_full"][my_key],
                    f"{where}: {len(kinds)} {my_key} leads in the view, more than the "
                    f"{mine['leads_full'][my_key]} it has in the whole genome")
            leads[my_key] = {"positions": [lead["position"] for lead in record["leads"]],
                             "clumps": [lead["clump"] for lead in record["leads"]], "kinds": kinds,
                             "leads": len(kinds), "spurious_splits": kinds.count("spurious_split"),
                             "lost": record["lost"]}
        rows = region["rows"]
        for position, abs_z, clump, index in zip(rows["position"], rows["abs_z"], rows["clump"],
                                                 rows["index_variant"], strict=True):
            lines.append(f"{analysis['analysis_id']}\t{position}\t{abs_z!r}\t{clump}\t{int(index)}")
        clumps = region["clumps"]
        first, second = (clumps + [None, None])[:2]
        widest = max(clumps, key=lambda c: c["end"] - c["start"])
        not_drawn = region["not_drawn"]
        views.append({
            "analysis": {k: analysis[k] for k in ("analysis_id", "analysis_label", "store", "z_band", "max_abs_z",
                                                  "index_variants")},
            "chosen": ({"as": "named"} if key == (named["store"], named["analysis_id"]) else
                       {"as": "drawn", "z_band": drawn[key]["z_band"], "eligible": len(drawn[key]["eligible"])}),
            "window": region["window"], "view": region["view"],
            "span_bp": region["view"]["end"] - region["view"]["start"],
            "rows_drawn": len(rows["position"]),
            "rows_in_view": len(rows["position"]) + not_drawn["rows"],
            "index_in_view": sum(bool(i) for i in rows["index_variant"]),
            "not_drawn": not_drawn,
            "clumps": clumps,
            "widest_clump": widest["clump"],
            "second_clump": None if second is None else {
                "clump": second["clump"], "distance_bp": abs(second["index_position"] - first["index_position"]),
                "without_lead": [k for k, v in leads.items() if first["clump"] in v["clumps"]
                                 and second["clump"] not in v["clumps"]]},
            "same_for_every_rule": len({(v["leads"], v["lost"]) for v in leads.values()}) == 1,
            "leads": leads,
            "blocks": region["blocks"],
            "profile": region["profile"],
        })
    tsv = "\n".join(lines) + "\n"
    tiers = regions["tiers"]
    return {
        "rule": {"analyses": regions["definitions"]["analyses"], "window": regions["definitions"]["window"],
                 "view": regions["definitions"]["view"], "seed": regions["selection"]["seed"],
                 "min_index_variants": regions["selection"]["min_index_variants"]},
        "tiers": tiers,
        "z_at_rows_tier": statistics.NormalDist().inv_cdf(1 - tiers["rows"] / 2),
        "z_at_profile_tier": statistics.NormalDist().inv_cdf(1 - tiers["profile"] / 2),
        "views": views,
        "rows": {"lines": len(lines) - 1, "sha256": hashlib.sha256(tsv.encode()).hexdigest()},
    }, tsv


def extract_regions(engine: Engine, counts: dict[str, Any]) -> tuple[dict[str, Any], str]:
    regions, _ = engine.read("eur_regions.json", "#409")
    sweep, sweep_sha, selection = eur_inputs(engine)
    profile, profile_sha = engine.read("eur_profile.json", "#409")
    return regions_record(regions, sweep=sweep, sweep_sha=sweep_sha, profile=profile, profile_sha=profile_sha,
                          selection=selection, counts=counts)


def build(engine: Engine, panel: Panel) -> tuple[dict[str, Any], str]:
    stage = "method"
    try:
        method = extract_method(engine)
        stage = "#402"
        tracer = extract_tracer(engine)
        stage = "#403"
        eur = extract_eur(engine)
        stage = "#404"
        maps = extract_maps(engine)
        stage = "#405"
        ancestries = extract_ancestries(engine, eur)
        stage = "#406"
        cost = extract_cost(engine)
        stage = "#409"
        profile = extract_profile(engine)
        stage = "the per-Analysis counts"
        counts = extract_counts(engine, panel)
        stage = "the regions"
        regions, rows = extract_regions(engine, counts)
    except KeyError as missing:
        raise Refused(f"refused: reading {stage}'s outputs, an engine output has no key {missing}; "
                      "nothing written") from missing
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
        "per_analysis_counts": counts,
        "regions": regions,
    }, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", type=Path, required=True, help="an opengwasdb checkout holding --commit")
    parser.add_argument("--commit", required=True, help="the engine commit whose outputs are read")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--regions-rows", type=Path, required=True,
                        help="the TSV of the rows the region figures draw, written beside the extract")
    args = parser.parse_args(argv)
    extract, rows = build(Engine(args.engine, args.commit), Panel())
    extract["regions"]["rows"]["file"] = args.regions_rows.name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.regions_rows.parent.mkdir(parents=True, exist_ok=True)
    args.regions_rows.write_text(rows, encoding="utf-8")
    args.output.write_text(json.dumps(extract, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: {len(extract['sources'])} sources at {extract['engine']['commit']}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
