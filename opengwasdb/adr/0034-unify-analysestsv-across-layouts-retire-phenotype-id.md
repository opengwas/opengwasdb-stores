# One `analyses.tsv` schema for every layout; retire `phenotype_id`/`phenotype_label`; add Attribution Metadata

Supersedes the `analyses.tsv` column list in ADR-0030 and store-format spec
§7a; does not reopen ADR-0030's core decision (Analytical Metadata lives
entirely in `analyses.tsv`, one row per Analysis). This is a **breaking format
change**, not an additive one — see Decision.

## Context

Three gaps surfaced together while scoping issue #63 (bring Ragged onto the
`analyses.tsv` contract, itself deferred out of PR #61/#53):

1. **Ragged's Analytical Metadata still lives in a divergent SQLite `analyses`
   table**, not `analyses.tsv` — a gap ADR-0030 flagged in its own
   Consequences section but never tracked to a filed issue. Ragged's schema
   (`trait_id`, `gene_id`, `gene_name`, `tissue`, `context`, `trait_chr`,
   `trait_bp`) has no equivalent columns in Dense/Hybrid's `analyses.tsv`, and
   nothing about most of those fields is actually layout-specific —
   `build_besd.py` and `build_ssf.py` already populate genomic position and
   gene identity per-Analysis today, and a Dense-stored molecular batch could
   equally have them. The only thing genuinely specific to Ragged is the CSR
   offsets array in `data.zarr/ragged`, which is physical storage, not
   metadata.

2. **`phenotype_id`/`phenotype_label` (`SHARED_CORE_COLUMNS` today) have no
   real contract and turned out not to serve one.** `opengwasdb-stores#63`
   asked whether they're meant to hold an ontology term (e.g. EFO) and noted
   this "isn't done in ukb-b." Checking the actual pipeline confirms why:
   `opengwasdb/build/source.py`'s field resolver falls back
   `phenotype_id → trait_id` when no distinct `phenotype_id` column exists in
   the source manifest, and `opengwasdb/layouts/dense/build_vcf.py` populates
   `phenotype_id`/`phenotype_label` directly from `row.trait_id`/`row.trait_name`
   — every real Dense store built this way has `phenotype_id` holding a raw
   trait identifier, never an ontology term. `overview.html` already treats
   `phenotype_label` and `analysis_label` interchangeably as a display
   fallback chain. CONTEXT.md's own glossary already says *"Trait ... Avoid:
   Phenotype, except as a synonym in user-facing text"* — the `phenotype_*`
   column names have been contradicting the project's own glossary since
   ADR-0030.

3. **A Store Release's self-containment goal (ADR-0030: "remains
   interpretable without a catalogue service") covers statistical
   interpretation but not usability.** `license`, `publication_doi`,
   `publication_pmid`, and `consortium` already exist as
   `REGISTRY_ONLY_COLUMNS` — tracked per-Analysis in the `opengwasdb-stores`
   registry, but never carried into the store itself. A downloaded store
   whose statistics are perfectly interpretable but that nobody can
   legally use or properly cite has failed self-containment just as surely as
   one with an undocumented effect scale — this just isn't the failure mode
   ADR-0030's "affects the interpretation of association statistics"
   definition of Analytical Metadata was written to cover.

A first pass at this decision proposed keeping a `trait_id` column (a raw,
source-native, non-unique identifier) alongside the new
`trait_ontology_id`/`trait_ontology_label`. Review caught that this recreates
exactly the ambiguity that made `phenotype_id` misleading in the first place:
`trait_id` would duplicate, depending on context, either `analysis_id`, an
unmapped `trait_ontology_id`, or nothing at all — the same "which of these
three overlapping columns is the real one" problem. Resolved by dropping it;
see Decision.

## Decision

**One `analyses.tsv` schema, used identically by Dense, Ragged, and Hybrid,
as a breaking format reset — not an additive, backward-compatible change.**
OpenGWASDB is pre-release: there is no requirement to keep reading
`phenotype_id`/`phenotype_label`-shaped `analyses.tsv` files, and no
requirement to keep supporting Ragged stores whose Analytical Metadata is
SQLite-only. Format docs, validators, and builders move together; a store
built before this change is simply not a valid store after it, exactly as
after any other pre-release breaking format revision. No dual-format reading
path, no compatibility shim, no migration-on-read.

Every column beyond `analysis_index`/`analysis_id` stays independently
optional per row (the existing blank-is-honest convention: a GWAS-phenotype
row leaves `gene_id`/`trait_chr`/etc. blank, a Trait with no ontology mapping
yet leaves `trait_ontology_id` blank) — layout no longer determines which
columns a store's metadata format has, only which rows populate which of them.

```text
# Identity — required column, required value, every layout. The one
# within-store identity key; unique within a Store Release.
analysis_index
analysis_id

# Trait identity — required columns; per-row values may be blank
analysis_label                 [unchanged: free-text, non-unique]
trait_ontology_id              [promoted from REGISTRY_ONLY_COLUMNS's trait_ontology_id;
                                 CURIE format, e.g. EFO:0001073; blank when unmapped;
                                 not unique -- several Analyses (BMI-in-males,
                                 BMI-in-females) may share one]
trait_ontology_label           [promoted from REGISTRY_ONLY_COLUMNS's trait_ontology_name]
gene_id                        [promoted from Ragged's SQLite analyses table]
gene_name                      [promoted from Ragged's SQLite analyses table]
tissue                         [promoted from Ragged's SQLite analyses table]
context                        [promoted from Ragged's SQLite analyses table]

# Genomic position of the trait -- optional; molecular traits have one
# (TSS, CpG site, ...), phenotype-level Analyses usually don't. Same single
# Reference Assembly the store's manifest already declares for variant
# coordinates (CONTEXT.md's Reference Assembly entry widens to cover this).
trait_chr                      [promoted from Ragged's SQLite analyses table]
trait_bp                       [promoted from Ragged's SQLite analyses table]

# Effect scale
stored_effect_scale            [becomes per-Analysis for Ragged too -- see
                                 Consequences; was store-wide via manifest
                                 provenance for build_ssf.py]
original_effect_scale
original_sd
original_sd_method
original_sd_dispersion

# Ancestry
assigned_ancestry
ancestry_assignment_method
ancestry_prop_<population>

# Sample size
sample_size_kind
sample_size_scope
sample_size
n_cases
n_controls

# Attribution Metadata (new category, see below) -- promoted from
# REGISTRY_ONLY_COLUMNS, plus one new column
license
publication_doi
publication_pmid
consortium
first_author                   [new]

# Reference completion (Reference-Completed releases only)
completed_against
completion_median_pearson_r
completion_n_imputed_total
completion_n_missing_total

# Top-Hit Counts -- now populated for Ragged too, closing issue #53's
# deferred acceptance criterion
n_hits_5e8
n_hits_5e6
n_hits_5e4
```

**Removed:** `phenotype_id`, `phenotype_label`, and Ragged's `trait_id`.
Nothing replaces `trait_id` under any name. **Cross-store Trait matching is
`trait_ontology_id`, or it is unavailable — there is no fuzzy fallback
identifier in the schema.** A caller matching Traits across two Store
Releases uses `trait_ontology_id` when both sides have curated one; when
either side's is blank, there is no store-schema-level way to match them, by
design — inventing a raw-identifier fallback is exactly the ambiguity this
ADR removes. If a real, specific need for a non-unique source-native
identifier surfaces later, it gets added explicitly as a new column with a
documented purpose (e.g. `source_trait_id`) — not resurrected as a
general-purpose `trait_id`.

One consequence: query methods that today resolve an identifier against
either `analysis_id` or `trait_id` (Ragged's `_resolve_analysis_id`) resolve
by `analysis_id` only once `trait_id` is gone. Callers who want "every
Analysis for this gene across tissues" compose that from `analyses_table()`
(filtering rows by `gene_id`, which is retained) rather than from a
single-identifier lookup that silently matched two different kinds of key.

**`REGISTRY_ONLY_COLUMNS` loses:** `trait_ontology_name`, `trait_ontology_id`,
`license`, `publication_doi`, `publication_pmid`, `consortium` (promoted to
store-scoped above). Everything else in that tuple (`checksum`, `source_file`,
`source_genome_build`, `analysis_group_id`, `inclusion_reason`,
`exclude_from_build`, ...) stays registry-only build/process provenance —
distinct from both Analytical Metadata (interpretation-bearing) and
Attribution Metadata (usability/citation-bearing) by CONTEXT.md's existing
"build provenance ... stays registry-scoped" line.

**New CONTEXT.md glossary term, Attribution Metadata:** metadata establishing
how an Analysis may be cited, licensed, and attributed (license,
publication DOI/PMID, consortium, first author). Distinct from Analytical
Metadata (affects statistical interpretation) and from build provenance
(checksums, generator versions) — still required for a Store Release to be
usable standalone, which is the same self-containment goal ADR-0030 stated
for Analytical Metadata, just for a different failure mode.

**Ragged drops its SQLite `analyses` table entirely, with no fallback read
path.** `RaggedStoreQuery` reads `analyses.tsv` into an in-memory dict at
store-open, the same pattern `AnalysesIndex` already uses for Dense/Hybrid
(ADR-0030 already argued this is equal-or-lower cost than the SQL path it
replaced there). Ragged's genomic range queries (`range_by_analysis`) keep
using a tabix-indexed positional side-file — this already exists and works
(`opengwasdb/traits/axis.py`'s `traits.tsv.gz`) and is unaffected by this
change; it is a query-acceleration structure over `analyses.tsv`'s content,
not a second copy of Analytical Metadata living in a second file with its own
column set as it effectively does today. `idx_analyses_gene_id`, created
today but never queried by anything in this codebase, is not carried forward.

## Alternatives considered

**Keep a `trait_id` column (raw, source-native, non-unique) alongside
`trait_ontology_id`.** Rejected on review: it duplicates, depending on
context, `analysis_id`, an unmapped `trait_ontology_id`, or nothing — the same
"which column is authoritative" ambiguity that made `phenotype_id` misleading.
`gene_id` already covers the one concrete case (grouping Analyses by measured
gene) that motivated wanting it.

**Keep `phenotype_id`/`phenotype_label` alongside the new columns.**
Rejected: checking the pipeline shows `phenotype_id` has never actually held
anything distinct from what other columns already hold. Keeping it would mean
maintaining a column with no distinct contract — the exact problem this ADR
exists to fix.

**Preserve backward-compatible reading of old-format `analyses.tsv`/SQLite-only
Ragged stores.** Rejected on review: OpenGWASDB is pre-release, so there is no
installed base whose stores must keep validating. Spending implementation and
test effort on dual-format support has no payoff at this stage; the correct
move for anyone holding an old-format store is to rebuild it against the
current format, exactly as for any other pre-release format revision.

**Require `trait_ontology_id` to be populated (hard validation gate).**
Rejected: ontology curation (matching a Trait to an EFO term) isn't always
immediately available and would block a release for Traits with no clean
mapping yet. Following the existing `original_sd_method`/`sample_size_*`
precedent, the column is required to exist; a blank value is an honest
"not yet mapped," not a fabrication.

**Leave license/DOI/consortium registry-only, per ADR-0030's original
Analytical-Metadata-only self-containment scope.** Rejected: a store that
cannot be legally used or properly cited outside the registry has failed
self-containment in practice, even though it doesn't affect how any one
Z/SE value is read. This is a different failure mode from what Analytical
Metadata covers, which is why it gets its own category rather than being
folded into that definition.

**Store license/attribution once per Store Release instead of per-Analysis.**
Rejected: a Hybrid or aggregated store (e.g. an `ieu-a`/`ieu-b` batch) can
combine Analyses from different source papers/consortia with different
licenses within one release; a single store-wide value would misattribute
some of them.

## Consequences

- Touches every layer that currently reads or writes `phenotype_id`/
  `phenotype_label`/`trait_id`/the Ragged SQLite schema: `opengwasdb/build/source.py`
  (field resolver + `NormalisedAssociation`), `opengwasdb/layouts/dense/build.py`
  (`AnalysisMetadata`), `layouts/dense/build_vcf.py`, `layouts/dense/overview.py`,
  `opengwasdb/query/facade.py` (`StoreQuery`/`HybridStoreQuery`/`RaggedStoreQuery`
  — the last needing its id-lookup and metadata-table methods rewritten off
  SQL, and its `analysis_id`-or-`trait_id` dual lookup narrowed to
  `analysis_id` only), `opengwasdb/model/analyses.py` (`SHARED_CORE_COLUMNS`/
  `STORE_ONLY_COLUMNS`/`REGISTRY_ONLY_COLUMNS`), `opengwasdb/layouts/ragged/`
  (`build_besd.py`, `build_ssf.py`, `complete.py`, and `analyses_schema.py`,
  which is retired), `docs/spec/store-format.md` §7a, and `CONTEXT.md`.
- `stored_effect_scale` becomes per-Analysis for Ragged, not store-wide via
  manifest provenance as `build_ssf.py` does today — a real behaviour change
  to that build path, not just a schema rename, though every source row seen
  so far has used one scale per build so no existing data is misrepresented
  by making the column explicit per-row.
- **The `opengwasdb-stores` registry repository needs matching changes** —
  its manifest schema and validation currently expect `phenotype_id`/
  `phenotype_label`/registry-only `license`/`trait_ontology_id` etc. This ADR
  cannot enact that side; it is a coordinated but separate piece of work.
  `opengwasdb-stores#63` (the `phenotype_id`/`trait_id` question) is closed by
  this decision; the registry-side schema change needs its own issue there.
- **No backward compatibility.** Stores (any layout) built before this change
  land are not valid stores after it. `scripts/migrate_store_to_analyses_tsv.py`
  needs updating to target the new schema if it is still needed for genuinely
  pre-ADR-0030 stores; it does not need to also handle the
  `phenotype_id`-era or old-Ragged-SQLite formats as separate legacy cases to
  preserve indefinitely.
