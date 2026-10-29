# Standardise continuous-trait effects to SD units via phenotype-SD rescaling

ADR-0004 assigns Stored Effect Scale purely from the source `StudyType` label, and
ADR-0005 deliberately keeps `z`/`se` reconstruction independent of allele frequency
and sample size. Together these mean a Continuous study is labelled `sd`
without ever being verified or actually standardised — the store trusts that the
source's reported units already are per-SD. That trust is not safe: `ieu-a-7`
(Coronary Heart Disease, unambiguously a case-control trait) carries
`StudyType=Continuous` in its VCF `##SAMPLE` header, with no `TotalCases`/
`TotalControls` field at all. Both the effect-scale label and the sample size for
this corpus must come from authoritative per-study metadata (the ingest
manifest/registry), never from the VCF header alone.

## Decision

Standardise continuous-trait effects at ingest by rescaling the source statistics
with a per-study phenotype SD: `stored_se = original_se / sd`, with `stored_beta`
following automatically since `z = beta/se` is invariant to dividing both by the
same constant. This is an exact linear rescaling given a correct `sd`, not a
reconstruction from `z`, `N`, and AF via the small-effect OLS approximation
(`se_sd ≈ 1/√(2f(1−f)(N+z²))`) — that formula is only ever an approximation of
dividing by the true `sd`, and its per-variant AF/`z²` noise makes it a worse
estimate than a single robust per-study scalar.

`sd` is obtained by the best available method, recorded per Analysis as
`original_sd_method`, in priority order:

1. `declared_standardised` — the source's own metadata declares the statistics
   are already on the SD scale (for example, the OpenGWAS API's `unit` field
   reporting `SD` for a study, independent of the source file's own header),
   so no rescaling is applied and no `original_sd` magnitude is recorded.
   Distinct from `source_provided` below: this is a scale declaration, not a
   phenotype-SD value to divide by.
2. `source_provided` — the source declares phenotype SD directly (including the
   `sd = 1` case when the source declares rank-inverse-normalisation).
3. `estimated_from_source_maf` — a robust, median-based estimator over the
   study's own observed variants: `sd_hat = median(se_raw · √(2·f·(1−f))) · √N`,
   the same formulation as `scalar_n_se` (`opengwasdb/completion/impute.py`),
   repurposed from reference-completion's per-study SE-scale constant to a
   phenotype-SD estimate.
4. `estimated_from_reference_maf` — the same estimator, substituting the
   ancestry-matched Ancestry Reference Panel frequency where source AF is absent.
5. `estimated_from_beta_distribution` — recovering `sd` from the spread of random
   common-variant betas when neither AF source is usable; the lowest-confidence
   rung, restricted to common-variant random samples.
6. `binary_trait` — not applicable; case-control effects are `log_or`, not
   SD-rescaled.
7. `unavailable` — `N` could not be established from any source; the study is
   flagged rather than guessed at, since the formula only ever yields `sd/√N` as
   a combined quantity without `N`.

A dispersion diagnostic (the spread — e.g. MAD — of the per-variant implied-`sd`
values behind methods 3-5, the three methods that actually compute a per-variant
estimate) is recorded alongside `original_sd` and `original_sd_method` as
Analytical Metadata (ADR-0030): high dispersion signals wrong AF orientation,
mixed ancestry, or genomic-control problems even though the median still
returns a value. `declared_standardised` and `source_provided` have no
per-variant estimate to disperse; `binary_trait` and `unavailable` produce no
`sd` at all.

## Considered options

- **Reconstruct `beta_sd`/`se_sd` per-variant from `z`, `N`, and AF.** Rejected as
  the primary method: correct in expectation but noisy per-variant (own AF error,
  reference-AF substitution error, and a `z²` term that behaves badly near
  genome-wide-significant hits), where a single robust per-study scalar is not.
- **Trust the source `StudyType` label with no standardisation (status quo).**
  Rejected: `ieu-a-7` demonstrates the label is not reliable for this corpus, and
  "Continuous" never implied "already SD-scaled" in the first place.

## Consequences

- `N` and case/control status must be resolved from the ingest manifest/registry
  metadata per Analysis, not read from the VCF `##SAMPLE` header, for every source
  in scope (OpenGWAS, FinnGen, EBI GWAS Catalog).
- `log_hazard` remains unimplemented: no source currently in scope is confirmed to
  report genuine hazard-ratio statistics rather than logistic effects.
- The estimator in `scalar_n_se` gains a second caller (phenotype-SD estimation at
  ingest) beyond its original reference-completion use; both read the same
  per-study SE-scale constant for different purposes.
