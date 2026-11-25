#!/usr/bin/env python3
"""Build, rebuild and accept the HGDP+1kGP LD Stores and Genotype Stores (#541; docs/spec/ld-store-registry.md).

    pixi run ld-stores build --kind ld_store|genotype_store --panel AMR --out DIR --ld-id OGL-NNNNN
    pixi run ld-stores rebuild OGL-00009 --out DIR
    pixi run ld-stores compare ORIGINAL REBUILT [--keep KEEP]

`build` stages a Genotype Store's inputs, runs `opengwasdb ld build` and
`opengwasdb ld validate`, and writes `stores/ld/<ld_id>/record.yaml` with the
build's `rebuild`. A person then writes the record's label, status, licence and
use. Its inputs are the plink2 conversion's (opengwasdb's
`scripts/ld/build_hgdp1kgp.py convert`), from `$OPENGWASDB_LD_PGEN_V2`,
`$OPENGWASDB_LD_KEEP` and `$OPENGWASDB_LD_PCS`, or the flags.

`rebuild` rebuilds a recorded Store into a new directory by running its
record's `rebuild` commands, which validate it once (a `validate` is added when
the record has none). Then it runs the acceptance, without validating again:
`opengwasdb ld info` of both compared field by field, and what the rebuilt
Store's provenance must record (n against its keep file). It writes no record.
It exits 1 on any difference outside the build's own fields, or any problem.

`compare` is the acceptance of two Stores already built, without a record: the
comparison, and what the rebuilt one's provenance must record, n against
`--keep` when given.

Each refuses an output that exists. Every engine command is printed with its
time, and runs in build92.sh's environment (one BLAS thread, a `TMPDIR` beside
the Store); run the tool under `nohup` or a terminal multiplexer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ld_registry import REPO_ROOT, compare, rebuild, recipe, record


def report(found: list[compare.Difference], problems: list[str]) -> int:
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    print(compare.render(found) or "no difference")
    failed = compare.failures(found)
    for difference in failed:
        print(f"FAIL {difference.field}: {difference.original!r} -> {difference.rebuilt!r}", file=sys.stderr)
    own = sum(1 for difference in found if difference.build_own)
    tolerated = sum(1 for difference in found if difference.within_tolerance)
    print(
        f"{len(failed)} failing difference(s), {own} of the build's own, {tolerated} within tolerance, "
        f"{len(problems)} other problem(s)"
    )
    return 1 if failed or problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ld-stores", description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    host = recipe.host_inputs()

    build = commands.add_parser("build", help="build a panel's Store, validate it and write its record")
    build.add_argument("--kind", required=True, choices=record.KINDS)
    build.add_argument("--panel", required=True, choices=recipe.PANELS)
    build.add_argument("--out", required=True, type=Path, help="the directory the Store is built in")
    build.add_argument("--ld-id", required=True, help="the record's id, OGL-NNNNN")
    build.add_argument("--threads", type=int, help="default: the kind's, 16 for an LD Store and 8 for a Genotype Store")
    build.add_argument("--pgen-root", type=Path, default=host.pgen_root)
    build.add_argument("--keep-root", type=Path, default=host.keep_root)
    build.add_argument("--pcs-root", type=Path, default=host.pcs_root)
    build.add_argument("--ld-stores-root", type=Path, default=host.ld_stores_root,
                       help="the published LD Stores, whose source.sha256 a Genotype Store's staged copies must match")
    build.add_argument("--ld-root", type=Path, default=REPO_ROOT / record.LD_ROOT)

    again = commands.add_parser("rebuild", help="rebuild a recorded Store into a new directory, and accept it")
    again.add_argument("ld_id")
    again.add_argument("--out", required=True, type=Path, help="a new directory, outside every recorded Store")
    again.add_argument("--ld-root", type=Path, default=REPO_ROOT / record.LD_ROOT)

    both = commands.add_parser("compare", help="compare two Stores' `opengwasdb ld info`")
    both.add_argument("original", type=Path)
    both.add_argument("rebuilt", type=Path)
    both.add_argument("--keep", type=Path, help="the keep file the rebuilt Store's n must equal")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            inputs = recipe.Inputs(args.pgen_root, args.keep_root, args.pcs_root, ld_stores_root=args.ld_stores_root)
            plan = recipe.plan_build(args.kind, args.panel, args.out.absolute(), inputs, args.threads)
            written = recipe.build_store(plan, args.ld_root, args.ld_id)
            print(f"wrote {written.path}")
            for error in record.load_record(written.path)[1]:
                print(f"  a person writes: {error}")
            return 0
        if args.command == "rebuild":
            plan = rebuild.rebuild_store(args.ld_root, args.ld_id, args.out)
            return report(*compare.accept(plan.original, plan.store, recorded_sha256=plan.recorded_sha256, keep=plan.keep))
        return report(*compare.accept(args.original, args.rebuilt, keep=args.keep))
    except (recipe.RecipeRefusal, rebuild.RebuildRefusal) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
