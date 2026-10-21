#!/usr/bin/env python3
"""Phase A operator entry point: `pixi run release` and `pixi run release-dry` (#195).

A thin launcher for `ogstores.release`, which wraps one Snakemake run with the
release guard: recovery, preflight refusal, records snapshot and settlement.
Running `snakemake --snakefile workflow/Snakefile` directly bypasses it.
"""

import sys

from ogstores.release import main

raise SystemExit(main(sys.argv[1:]))
