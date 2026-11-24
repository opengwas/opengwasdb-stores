#!/usr/bin/env python3
"""Pick the yardstick's fixed, scattered inputs for one Store, read-only (#503).

    pixi run -e dev python3 tools/yardstick_inputs.py OGS-00009 [--stores-root DIR]

Everything is read through the engine's facade (`opengwasdb.query.query_store`):
`variants_table()` and `analyses_table()` give the rows, and one call per input
proves it returns rows. The result is a YAML fragment for the Store's entry in
`resources/yardstick/workload.yaml`: `lookup`, `region_100kb`, `top_hits`, `cis`
(Ragged only), `rho`, and `input_rows`, the row count each input returned. The
entry's existing `rsid`, `alid`, `position`, `analysis_id` and `region` are read
from the workload and counted too.

Variants and Analyses are picked at evenly spaced rows of the Store's own
tables, 100 of each, so they spread over every chromosome; a lookup's smaller
shapes are the first 1 and first 10 of those lists. Nothing is written under
the Artifact Root: the Store's top-level entries and `index.sqlite` are compared
before and after, as the runner does.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from ogstores import paths
from yardstick import runner, workload as workload_spec
from yardstick.measure import open_store, rows
from yardstick.workload import LOOKUP_N, LOOKUP_SHAPES, lookup_inputs

THRESHOLD = 5e-8
CIS_HALF_WIDTH_BP = 500_000
#: Analyses tried, in order, for `top_hits` before the Store is recorded as having none there.
TOP_HITS_CANDIDATES = 5


def evenly_spaced(keys: list[int], n: int = LOOKUP_N) -> list[int]:
    """`n` keys at evenly spaced positions across the whole sorted list, each in the middle of its stripe."""
    if len(keys) < n:
        raise ValueError(f"only {len(keys)} rows, need {n}")
    return [keys[(i * len(keys)) // n + len(keys) // (2 * n)] for i in range(n)]


def centred(region: dict[str, Any], width: int) -> dict[str, Any]:
    middle = (region["start"] + region["end"]) // 2
    return {"chromosome": region["chromosome"], "start": max(1, middle - width // 2), "end": middle + width // 2}


def top_hits_path(store_id: str) -> str:
    """`index` if the Store's build.yaml says it built the top-hit index (`post.top_hits`), else `scan`."""
    build = yaml.safe_load((workload_spec.REPO_ROOT / "stores" / store_id / "build.yaml").read_text(encoding="utf-8"))
    return "index" if build["post"]["top_hits"] else "scan"


def pick(q: Any, entry: dict[str, Any]) -> dict[str, Any]:
    variants = q.variants_table()
    analyses = q.analyses_table()
    analysis_ids = [analyses[i]["analysis_id"] for i in evenly_spaced(sorted(analyses))]
    variant_keys = sorted(variants)
    if entry["layout"] == "ragged":
        # A Ragged Store holds each Analysis only inside its own window, so variants scattered over the whole
        # table meet almost no Analysis (1x100 and 100x1 returned nothing on OGS-00027). Scatter them over the
        # variants the picked Analyses hold instead: still evenly spaced, still across the chromosomes.
        held: set[int] = set()
        for analysis_id in analysis_ids:
            held.update(int(v) for v in q.analysis(analysis_id).variant_index)
        variant_keys = sorted(held)
    lookup = {
        "variants": [variants[i]["alid"] for i in evenly_spaced(variant_keys)],
        "analyses": analysis_ids,
    }
    del variants
    counts: dict[str, int] = {}
    counts["phewas_rsid"] = rows(q.phewas(entry["rsid"]))
    counts["phewas_alid"] = rows(q.phewas(entry["alid"]))
    pos = entry["position"]
    counts["phewas_position"] = rows(q.range_phewas(pos["chromosome"], pos["position"], pos["position"]))
    counts["analysis_full"] = rows(q.analysis(entry["analysis_id"]))
    if entry["measures"].count("analysis_full_observed_only"):
        counts["analysis_full_observed_only"] = rows(q.analysis(entry["analysis_id"], observed_only=True))
    region = entry["region"]
    counts["region"] = rows(q.range_phewas(region["chromosome"], region["start"], region["end"]))
    small = centred(region, 100_000)
    counts["region_100kb"] = rows(q.range_phewas(small["chromosome"], small["start"], small["end"]))
    # `resolve` is timed on the rows of `phewas(rsid)`: its own call resolves them and counts what it yields.
    counts["resolve"] = len(list(q.resolve(q.phewas(entry["rsid"]))))
    for name, (nv, na) in LOOKUP_SHAPES.items():
        counts[name] = rows(q.lookup(*lookup_inputs(lookup, nv, na)))
    out: dict[str, Any] = {"lookup": lookup, "region_100kb": small}

    candidates = [entry["analysis_id"], *lookup["analyses"]][:TOP_HITS_CANDIDATES]
    hits = {"analysis_id": entry["analysis_id"], "threshold": THRESHOLD, "rows": 0, "path": top_hits_path(entry["store_id"])}
    for analysis_id in candidates:
        n = rows(q.top_hits(analysis_id=analysis_id, threshold=THRESHOLD))
        if n:
            hits = {**hits, "analysis_id": analysis_id, "rows": n}
            break
    out["top_hits"] = hits
    counts["top_hits"] = hits["rows"]

    if entry["layout"] == "ragged":
        # A cis window around the entry Analysis's own trait position that holds Analyses.
        row = next(r for r in analyses.values() if r["analysis_id"] == entry["analysis_id"])
        bp = int(row["trait_bp"])
        window = {"chromosome": row["trait_chr"], "start": max(1, bp - CIS_HALF_WIDTH_BP), "end": bp + CIS_HALF_WIDTH_BP}
        out["cis"] = window
        counts["range_by_analysis"] = rows(q.range_by_analysis(window["chromosome"], window["start"], window["end"]))

    # Whether the Store has a Rho Matrix is declared by the entry (`rho: false`, or the ids), never probed.
    out["rho"] = False
    if entry["rho"]:
        ids = entry["rho"]["ids"]
        out["rho"] = entry["rho"]
        counts["rho"] = rows(q.rho(*ids))
        counts["rho_row"] = rows(q.rho_row(entry["rho"]["analysis_id"]))
        counts["rho_matrix"] = rows(q.rho_matrix())
    out["input_rows"] = counts
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("store_id")
    parser.add_argument("--stores-root", type=Path, default=paths.DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--workload", type=Path, default=workload_spec.DEFAULT_PATH)
    args = parser.parse_args(argv)
    entry = next(
        (s for s in yaml.safe_load(args.workload.read_text(encoding="utf-8"))["stores"] if s["store_id"] == args.store_id), None
    )
    if entry is None:
        print(f"error: {args.store_id} is not in {args.workload}", file=sys.stderr)
        return 1
    store_path = paths.store_dir(args.store_id, args.stores_root) / "store.opengwasdb"
    before = runner.snapshot(store_path)
    q = open_store(store_path)
    try:
        picked = pick(q, entry)
    finally:
        q.close()
    disturbed = runner.compare(before, runner.snapshot(store_path))
    if disturbed:
        print(f"error: the read changed the Store: {disturbed}", file=sys.stderr)
        return 3
    yaml.safe_dump({args.store_id: picked}, sys.stdout, sort_keys=False, default_flow_style=None, width=100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
