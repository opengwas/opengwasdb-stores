# AF-based ancestry assignment fixtures

`fixtures/` is a tiny synthetic ancestry-mixture reference -- four fine groups
(one per super-population EUR/AFR/EAS/SAS), forty diagnostic variants, the same
shape as the UK Biobank panel at `/data/opengwasdb/reference/ancestry-mixture/`
-- and a small release, regenerated deterministically by
`fixtures/generate_fixtures.py`.

The suite that tested `manifest_generation.providers.gwas_catalog.ancestry_assign`
against them retired with that module and the R GWAS-SSF generators that called
it (#300): Analysis Resolution now measures ancestry for every Analysis (ADR
0036). The fixtures stay because `tests/opengwas-gwas-vcf-dense/test_annotation.py`
and `tests/finngen-r13-pilot/test_annotation.py` annotate against the same
reference, each in its own temporary copy.
