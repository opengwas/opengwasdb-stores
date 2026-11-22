# Execution-only amendment: `n-workers: 10` for OGS-00011's build

Owner-authorized execution amendment, recorded 2026-11-22 (#478). The owner
chose to restart OGS-00011's forced rebuild with fewer workers after it
exhausted the host's memory. It changes **only** the worker count of the Build
Recipe's `build` phase in `stores/OGS-00011/build.yaml`, from 24 to 10, and the
generator config the bundle is emitted from
(`resources/generators/gwas-catalog-eur-hybrid/release-eur-hybrid-full.yaml`),
so a re-emit reproduces it.

## Why

The forced rebuild started at 17:59 UTC on 2026-11-22. After about 4 h,
`build-hybrid`'s 24 workers held 811 GB together (PSS plus swap, about 34 GB
each), and its parent held 75 GB. With the host's LD API workers holding a further
411 GB, the 1,006 GB host had 2 GB free, its 31 GB of swap was full, and the
kernel had already killed a 27 GB `opengwasdb` process on 7 Nov. The run was
stopped with SIGTERM and exited 143. The release restored the published Store's
records, and the published Store was not touched.

Ten workers at the measured peak of about 40 GB each, plus the parent, fit in
about 475 GB, against 548 GB available with the API running. The generator
config already records that Hybrid build throughput is flat above 8 workers
(#154).

## Why it is execution-only, not a new Store Release

As for [OGS-00012–16's `n-workers: 16`](ogs-00012-16-nworkers-16-amendment.md):
opengwasdb ADR 0053 states that a Hybrid build's `n_workers` is "a pure
runtime knob no computed value depends on", so the produced Store does not
depend on it and ADR 0004's material-choice test is not met. `analyses.tsv`,
`release.yaml` (status `accepted`), `validation.yaml` and every other Build
Recipe option are byte-unchanged.

The build-phase `validate` step takes the build's count (ADR 0023, stores#358),
so it also runs with 10 workers. The `complete` block keeps `n-workers: 24`:
completion caps its own pools at a quarter of `MemAvailable` (opengwasdb
ADR 0080), so it cannot over-commit the host in the same way. Its own
`completion-validate` keeps 24.
