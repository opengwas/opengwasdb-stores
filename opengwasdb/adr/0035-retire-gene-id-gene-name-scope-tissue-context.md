# Retire `gene_id`/`gene_name`; keep `tissue`/`context` scoped to what was measured

Amends ADR-0034 while opengwasdb is still pre-release: narrows the shared-core
column list ADR-0034 promoted to `analyses.tsv`, not its core decision (one
unified schema across every Primary Storage Layout).

## Context

ADR-0034 promotes `gene_id`, `gene_name`, `tissue`, and `context` to shared
core alongside `analysis_label` and `trait_ontology_id`/`trait_ontology_label`,
as independent Trait identity columns:

```text
analysis_label                 [unchanged: free-text, non-unique]
trait_ontology_id              [CURIE format, e.g. EFO:0001073; blank when unmapped]
trait_ontology_label
gene_id                        [promoted from Ragged's SQLite analyses table]
gene_name                      [promoted from Ragged's SQLite analyses table]
tissue
context
```

Building out Trait Ontology Mapping resolution on the registry side
(opengwasdb-stores) surfaced that, for gene-centric Analyses (pQTL/proteomics),
`gene_id`/`gene_name` don't carry any fact that `analysis_label` +
`trait_ontology_id` couldn't already carry between them: `analysis_label` can
be the gene symbol (free text, e.g. `PDK1`), and `trait_ontology_id` can be
the Ensembl gene ID (a controlled identifier, just not a disease-ontology
one) once it's understood as polymorphic-by-Trait-kind rather than
EFO/MONDO-only. No consumer of `gene_id`/`gene_name` as separate columns
exists yet in opengwasdb itself. Since opengwasdb is still pre-release, this
is the moment to simplify rather than carry two genuinely redundant columns
forward.

Separately, `tissue`/`context` are underspecified. opengwasdb-stores'
`gwas-ssf-ragged` generator already emits them today with real, working,
narrow content: `tissue=plasma`, `context=SomaScan` (pQTL) or
`context=metabolomics` (metabolome). If `context` is widened to also carry
GWAS method and covariates (a related, live discussion), it starts mixing
two different kinds of fact: *what was measured* (tissue, timepoint,
assay/platform -- a Trait identity concern) versus *how the association was
computed* (GWAS method, covariates -- an Analytical Metadata concern, per
ADR-0030's "affects the interpretation of association statistics"
definition). Conflating them into one field makes both harder to query later
(e.g. "find every liver Analysis" becomes a substring search against a blob
that might also contain "BOLT-LMM, age+sex+10PCs").

## Decision

**Retire `gene_id`/`gene_name` as dedicated columns.** For gene-centric
Analyses, identity is fully expressed by `analysis_label` (free-text gene
symbol) + `trait_ontology_id` (Ensembl gene ID, e.g.
`ENSEMBL:ENSG00000152256`) + `trait_ontology_label` (`"Ensembl"` or
equivalent). `trait_ontology_id`'s CURIE contract already tolerates
per-Trait-kind vocabularies (EFO/MONDO for phenotypes); this makes that
explicit rather than adding two more columns that duplicate the same fact.

**Keep `tissue`/`context` scoped to trait-measurement facts only** -- what
was measured: tissue, timepoint, assay/platform. **Do not** fold GWAS method
or covariates into `context`. Introduce those as their own, separately named
Analytical Metadata fields instead (e.g. `analysis_method`, `covariates`),
so `context` stays a queryable, single-purpose field rather than a catch-all.
(Exact shape of `analysis_method`/`covariates` -- free text vs. lightly
structured -- is not fully settled; flagging it here as a known follow-on
question rather than deciding it in the same breath as the `gene_id`/
`gene_name` retirement.)

## Consequences

- opengwasdb's `Analysis` dataclass / `ANALYSIS_COLUMNS` drop `gene_id`/
  `gene_name`. The Ragged builders (`build_besd.py`, `build_ssf.py`, via
  `layouts/ragged/analyses.py`'s `molecular_analysis()`) express gene identity
  through `analysis_label`/`trait_ontology_id`/`trait_ontology_label`
  instead: `build_besd.py` sets `analysis_label` from the EPI gene symbol and
  `trait_ontology_id`/`trait_ontology_label` from the probe's Ensembl gene ID
  when the EPI probe ID is one (`ENSEMBL:<id>` / `"Ensembl"`);
  `build_ssf.py`'s manifest reader carries `analysis_label`/
  `trait_ontology_id`/`trait_ontology_label` straight through from its input
  manifest in place of `gene_id`/`gene_name`.
- opengwasdb-stores' `gwas-ssf-ragged` generator currently emits `gene_id`/
  `gene_name` for its one gene-centric family (pQTL) and needs a
  corresponding change once this lands: stop emitting them, set
  `analysis_label` from the already-resolved `gene_name` (via
  `scripts/somascan/generate-somascan-analysis-targets.R`'s existing
  Ensembl resolution), and set `trait_ontology_id`/`trait_ontology_label`
  from the same resolved Ensembl identity. **Do not implement this in
  opengwasdb-stores until this ADR is decided and lands here** -- same
  sequencing discipline as opengwasdb-stores#63 waiting for ADR-0034 itself.
- No change needed to `tissue`/`context`'s existing shape or the two
  currently-built Store Families using them; this decision only blocks
  *widening* `context` to also cover method/covariates.

## Alternatives considered

- **Keep `gene_id`/`gene_name` as-is.** Rejected: redundant with
  `analysis_label`/`trait_ontology_id` once the latter is understood as
  polymorphic, and no real consumer exists yet to justify the extra surface
  while still pre-release.
- **Fold GWAS method/covariates into `context`.** Rejected: conflates Trait
  identity (what was measured) with Analytical Metadata (how it was
  analysed), degrading `context`'s queryability for both purposes.
