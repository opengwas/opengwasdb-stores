#!/usr/bin/env python3
"""Join acquired sample labels to the tracked population policy and write keep files."""
from __future__ import annotations
import argparse, csv, json
from pathlib import Path

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cohort", type=Path, required=True); ap.add_argument("--mapping", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    with args.mapping.open() as fh:
        policy_rows = list(csv.DictReader(fh, delimiter="\t"))
    policy = {r["population"]: r for r in policy_rows}
    if len(policy) != len(policy_rows): raise SystemExit("duplicate populations in mapping")
    handles = {p: (args.out / f"{p}.keep.tmp").open("w") for p in ("AFR","EAS","SAS","EUR")}
    # PLINK 2 interprets an unheaded two-column file as FID/IID. VCF imports
    # assign FID=0, so writing sample/sample silently matches no individuals.
    # A #IID header makes this an unambiguous one-column IID list.
    for fh in handles.values(): fh.write("#IID\n")
    counts = {p: 0 for p in handles}; seen = set()
    try:
        with args.cohort.open() as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                pop = row["population"] or "synthetic_diploid_truth_sample"; seen.add(pop)
                if pop not in policy: raise SystemExit(f"unmapped cohort population: {pop}")
                rule = policy[pop]
                if rule["status"] == "included" and row["high_quality"].lower() == "true":
                    handles[rule["panel"]].write(f'{row["sample"]}\n')
                    counts[rule["panel"]] += 1
    finally:
        for fh in handles.values(): fh.close()
    missing = set(policy) - seen
    if missing: raise SystemExit(f"mapping contains populations absent from cohort: {sorted(missing)}")
    for panel in handles: (args.out / f"{panel}.keep.tmp").replace(args.out / f"{panel}.keep")
    (args.out / "membership.json").write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
    print(json.dumps(counts, sort_keys=True)); return 0
if __name__ == "__main__": raise SystemExit(main())
