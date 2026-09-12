# FinnGen R13 pilot generator

This generator freezes a bounded, reproducible 20-Analysis trial: all three
inverse-rank-normalised quantitative endpoints plus 17 case-control endpoints
selected across distinct FinnGen manifest categories. It is evidence for a
full-release onboarding decision, not a claim that all R13 endpoints have been
ingested.

The provider manifest and summary-statistics artifacts live outside Git under
`/data/opengwasdb/finngen-r13/releases/r13-pilot-20/`. Fetch the manifest from
the URL in `config-pilot-20.yaml`, verify its pinned SHA-256, then run:

```sh
pixi run Rscript families/finngen-r13/generators/generate.R --mode=emit
pixi run python resources/generators/finngen-r13-dense/acquire.py \
  --release-dir=families/finngen-r13/releases/r13-pilot-20
pixi run python resources/generators/opengwas-gwas-vcf-dense/annotate.py \
  --release-dir=families/finngen-r13/releases/r13-pilot-20 \
  --workers=8
pixi run python resources/generators/finngen-r13-dense/assess.py \
  --release-dir=families/finngen-r13/releases/r13-pilot-20 \
  --full-analysis-count=2754
```

`--mode=emit` is this generator's only mode: it stops at the fixed input
(`analyses.tsv` plus the release's raw source files). There is no build mode.
The Store itself is built from that fixed input by the shared workflow, not by
any generator or family-specific script (issue #103):

```sh
pixi run release --configfile families/finngen-r13/releases/r13-pilot-20/build.yaml
```

`build.yaml` (written by the generator) names the raw source root, the manifest,
the `opengwasdb build-dense-vcf` command, and the optional rho and Reference
Completion branches; the workflow's validate phase reads the metadata back out
of the built Store. See `workflow/README.md`.

Acquisition uses `.part` files, HTTP Range requests, atomic promotion, and
manifest checksums, so interrupted and repeated runs are safe. The assessment
records the measured pilot costs and emits the evidence-based full-release
recommendation; a pilot can build successfully without receiving a full-release
GO recommendation.
