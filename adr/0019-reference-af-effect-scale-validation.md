# Reference-AF effect-scale validation as a generator stage

When a Source Collection provides no usable source allele frequencies, ancestry assignment stays `source_trusted_no_af` and `effect_scale: passed` previously meant only that declared controlled-vocabulary values were valid, not that the stored effects were empirically checked against their declared scale.

Reference-AF effect-scale validation is a `derive`/validation stage owned by the Manifest Generator, not a new Store Release identity rule or an ancestry-assignment method. It uses the Analysis's already-assigned ancestry to choose a declared Reference Resource, aligns alleles conservatively (excluding palindromic and mismatched pairs), and computes implied phenotype SD from standard error, sample size, and MAF. Source-provided allele frequencies are preferred whenever usable; reference AF is only a fallback. The result is recorded per-Analysis in an SD-estimation sidecar and rolled up into `validation.yaml` `checks.effect_scale`/`checks.sd_estimation`, which must therefore distinguish empirical validation from vocabulary-only validity.

This conditions effect-scale evidence on the current ancestry assignment without upgrading it: a reference-MAF match cannot itself prove the assigned ancestry is correct when source AF was absent, so ancestry assignment method is left unchanged by this stage.
