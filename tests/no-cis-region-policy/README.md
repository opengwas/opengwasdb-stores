# No-cis sparse-region policy tests

Fixture-based test for the `gwas-ssf-ragged` generator's no-cis sparse-region
policy (issue #26): Store Families with no single encoding gene per Analysis
(e.g. small-molecule metabolomics) omit `inputs.analysis_targets` and retain
only significant/suggestive regions, never a fabricated cis window.

Run from the repository root:

```sh
pixi run Rscript tests/no-cis-region-policy/run_tests.R
```

This regenerates a tiny synthetic "full" GWAS-SSF source file (5 clustered
significant hits, 3 well-separated suggestive hits, 20 null variants) served
over a local `file://` URL — no network access needed — and runs the real
generator (`emit` -> `validate` -> `filter`) against it, asserting on the
emitted release-bundle outputs: `analyses.tsv` has no single-gene-target
columns, `release.yaml` has no `sidecars.analysis_targets` pointer, and
`sidecars/sparse_regions.tsv` has zero `cis` rows and only the expected
significant/suggestive regions.

The fixture also emits a temporary gene-target Release Bundle without building
a Store, proving that the target path writes the source-provided ontology term
and trait label through `trait_ontology_id`/`trait_ontology_label`, records
`source_provided`, keeps the gene as target annotation (`trait_chr`/`trait_bp`),
and omits the upstream-retired `trait_id`/`gene_id`/`gene_name` Analysis columns
(issues #130 and #141). It covers three Analyses: a single-target Analysis
labelled by its gene symbol; an aggregate whose source label enumerates several
member symbols and is therefore labelled by its SomaScan SeqId; and an assay
flagged `somascan_is_multiple` in the shared target resource. Neither aggregate
promotes one member gene to the Trait identity. The existing pqtl-interval-2018
family's cis+signals selection policy remains unchanged.
