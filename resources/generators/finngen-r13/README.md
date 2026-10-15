# FinnGen R13 pilot generator

This generator freezes a bounded, reproducible 20-Analysis trial: all three
inverse-rank-normalised quantitative endpoints plus 17 case-control endpoints
selected across distinct FinnGen manifest categories. It is evidence for a
full-release onboarding decision, not a claim that all R13 endpoints have been
ingested.

The provider manifest and summary-statistics artifacts live outside Git under
`/data/opengwasdb/finngen-r13/releases/r13-pilot-20/`. Fetch the manifest from
the URL in `config-pilot-20.yaml`, verify its pinned SHA-256, then run:

The working bundle lives in the generator's work root,
`<artifact-root>/OGS-00003/generate` (#248):

```sh
WORK=$(pixi run python3 -m manifest_generation.emit work-root OGS-00003)
pixi run Rscript resources/generators/finngen-r13/generate.R --mode=emit
pixi run python3 -m manifest_generation.providers.finngen.acquire \
  --release-dir="$WORK"
pixi run python3 -m manifest_generation.providers.opengwas.annotate \
  --release-dir="$WORK" \
  --workers=8
pixi run python3 -m manifest_generation.providers.finngen.assess \
  --release-dir="$WORK" \
  --full-analysis-count=2754
```

Acquisition uses `.part` files, HTTP Range requests, atomic promotion, and
manifest checksums, so interrupted and repeated runs are safe. Building and
smoke queries use OpenGWASDB's existing Store envelope and public APIs. The
assessment records the measured pilot costs and emits the evidence-based
full-release recommendation; a pilot can build successfully without receiving
a full-release GO recommendation.

## Full release (`config-full.yaml`)

`config-full.yaml` is the production-scale sibling of the pilot: `selection.mode:
full` freezes all 2,754 endpoints of the same pinned public manifest, in the
manifest's own order, with no analysis cap. Every endpoint must still be
honestly classified by the metadata resolver (a quantitative endpoint with
`num_cases=N`, or a case-control endpoint with positive case and control
counts); an unclassifiable endpoint fails the emit rather than being dropped.
The generator is shared, not copied: `select_finngen_r13_full` sits beside
`select_finngen_r13_pilot` in `resources/generators/lib/finngen_r13_pilot.R`.

```sh
# 1. Freeze the full 2,754-Analysis Release Manifest (status: candidate).
pixi run Rscript resources/generators/finngen-r13/generate.R \
  --config=resources/generators/finngen-r13/config-full.yaml --mode=emit

# 2. Download every endpoint into the release artifact source directory.
#    Resumable: completed files are checksummed and kept, not re-fetched.
pixi run python3 -m manifest_generation.providers.finngen.acquire \
  --release-dir="$(pixi run python3 -m manifest_generation.emit work-root OGS-00017)" --workers=8
```

`src/manifest_generation/providers/finngen/acquire-finngen-r13-full.sh` wraps step 2 with a retry loop
for unattended tmux use.

## Emitting the registry Release Bundle (`emit-registry`)

The `emit` stage above writes a family-shaped working bundle into the work root,
`<artifact-root>/<OGS-id>/generate`, which `acquire.py` turns into checksummed
sources. The registry consumes the **current** bundle contract instead: a flat
`release.yaml`/`build.yaml`, the acquired `analyses.tsv` and its checksummed
sidecars, under `stores/<OGS-id>/`.

`--mode=emit-registry` projects that bundle from the acquired working bundle and
emits it through the one emitter (`src/manifest_generation/emit.py`, #248). It
is a pure projection: it re-resolves no row and re-downloads nothing, so it can
be re-run after any acquisition fix. It refuses to emit unless every row already
carries a source checksum, so a blank provenance row cannot be frozen into a
bundle, and the emitter replaces only an earlier candidate.

```sh
pixi run Rscript resources/generators/finngen-r13/generate.R \
  --config=resources/generators/finngen-r13/config-full.yaml --mode=emit-registry
pixi run bundle-check
pixi run index
```

The full release (`r13-full`, `OGS-00017`) emitted 2,754 Analyses in
`status: candidate`, since accepted; the emitter now refuses to replace it, so a
regenerated full release needs a new Store Release id. Acceptance is a separate
human review; nothing here accepts the bundle or invokes Phase A. The `build:` block in `config-full.yaml` carries
the observed-only dense recipe (`build-dense-vcf`, 32 workers) that Phase A will
plan from once the bundle is accepted.
