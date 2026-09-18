# eQTLGen cis-eQTL BESD -> Ragged pilot assessment

## Observed build

- Probes/Analyses: 10 (LPL, SORT1, APOE, GSDMB, ERAP2, TNF, TP53, IL6, CETP, MYC)
- GRCh38 union variants: 86,376
- Associations: 86,373
- Source subset: 4.5 MB (from a 2.3 GB source BESD triple, 19,250 probes)
- Store size: 8.5 MB
- Liftover hg19 -> hg38: 120/86,496 variants failed (0.1%), well under threshold
- Store validation (`opengwasdb.validation.validate_store`): passed
- Top hits: 9,507 at p<5e-8, 11,738 at p<5e-6, 16,861 at p<5e-4
- Read-back smoke probes: LPL (`ENSG00000175445::whole_blood`, 9,636 associations,
  top |z|=59.4) and SORT1 (`ENSG00000134243::whole_blood`, 6,119 associations) --
  both classic, strong cis-eQTL loci; results consistent with well-established
  eQTLGen biology.

## Release evidence

- Checks: files=passed, reader_smoke_test=passed, store=passed, schema=not_run
  (see `build.yaml`'s `source.source_reader_capability: null` and `generate.py`'s
  own comment: `opengwasdb.model.analyses.validate_analyses()` validates a
  registry-authored, pre-build manifest, which BESD sources have none of --
  `build_ragged_from_besd()` derives Analyses on the fly from the source
  `.epi` file, so there is nothing for that check to run against for this
  Source Format)
- Named warnings: none

## Pipeline notes

- `resources/generators/eqtlgen-besd-ragged/subset_besd.py` is new: opengwasdb's
  `BESDReader`/`build_ragged_from_besd()` already read/build BESD correctly,
  but had no *write* side for shrinking a genome-wide BESD source to a small
  pilot before handing it to that builder unmodified -- the same shape as
  `gwas-ssf-ragged`'s download+filter step for GWAS-SSF. Round-trip correctness
  (SNP remapping, shared-SNP-across-probes dedup, zero-association probes) is
  covered by `tests/eqtlgen-besd-ragged/test_subset_besd.py` against a small
  synthetic fixture; this release is the first run against real data.
- `opengwasdb.model.analyses.validate_analyses()`'s `REQUIRED_COLUMNS`
  (`stored_effect_scale`, `sample_size_kind`, `sample_size_scope`,
  `sample_size`, `original_effect_scale`, `ancestry_assignment_method`) are
  never populated by `build_ragged_from_besd()` for *any* BESD source, not
  something specific to this pilot -- confirmed by reading that builder
  directly. `opengwasdb.validation.validate_store()` (the correct acceptance
  gate for a *built* store) does tolerate this. Worth an opengwasdb-side issue
  if BESD/molecular-QTL Analytical Metadata passthrough is wanted later, in
  the same spirit as opengwasdb#83/#86 -- not filed here since nothing in this
  pilot's own source data (eQTLGen's BESD triple) carries case/control counts,
  sample-size structure, or an effect-scale declaration to pass through in the
  first place; there would be nothing to pass through except a fixed,
  registry-known constant (e.g. eQTLGen's published N=31,684, sd-scale
  standardised expression).

## Scale-up risks

- Linear source-volume extrapolation from 10 to eQTLGen's full 19,250-probe
  cis-eQTL catalogue is roughly 1,925x this pilot's associations (order
  166M), well within opengwasdb's demonstrated Ragged capacity at the
  `pqtl-interval-2018` family's own scale, but untested by this pilot itself.
- eQTLGen's cis-eQTL association effect sizes have no declared
  `stored_effect_scale`/sample-size metadata in the source BESD itself (see
  Pipeline notes) -- a full-catalogue release would need a registry-side
  decision on what (if anything) to declare for these fields, since
  opengwasdb's shared schema conventions (SD/log_or/log_hazard) do not map
  cleanly onto standardised gene-expression effect sizes.
- This pilot's probes were hand-picked for biological familiarity, not by any
  reproducible selection rule -- a full-catalogue release needs a real
  selection/prioritisation policy (mirroring `gwas-ssf-ragged`'s or
  `finngen-r13-dense`'s), not a hand-curated gene list.

## Decision

No blocking release checks failed.

Recommendation: **GO** for closing issue #67 (prove the BESD -> Ragged
pipeline with a small, real pilot) -- not a recommendation to onboard the
full eQTLGen cis-eQTL catalogue, which needs the scale-up work above first.
