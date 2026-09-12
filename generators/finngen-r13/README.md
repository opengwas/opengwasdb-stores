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
pixi run Rscript generators/finngen-r13/generate.R --mode=emit
pixi run python generators/lib/source-formats/finngen-r13-dense/acquire.py \
  --release-dir=families/finngen-r13/releases/r13-pilot-20
pixi run python generators/lib/source-formats/opengwas-gwas-vcf-dense/annotate.py \
  --release-dir=families/finngen-r13/releases/r13-pilot-20 \
  --workers=8
pixi run python generators/lib/source-formats/opengwas-gwas-vcf-dense/build-store.py \
  --release-dir=families/finngen-r13/releases/r13-pilot-20 \
  --workers=8
pixi run python generators/lib/source-formats/finngen-r13-dense/assess.py \
  --release-dir=families/finngen-r13/releases/r13-pilot-20 \
  --full-analysis-count=2754
```

Acquisition uses `.part` files, HTTP Range requests, atomic promotion, and
manifest checksums, so interrupted and repeated runs are safe. Building and
smoke queries use OpenGWASDB's existing Store envelope and public APIs. The
assessment records the measured pilot costs and emits the evidence-based
full-release recommendation; a pilot can build successfully without receiving
a full-release GO recommendation.
