# Canonical trait mapping (EFO)

A curated fallback for Trait Ontology Mapping (see `CONTEXT.md`) when a
Source Collection doesn't supply its own `trait_ontology_id`/
`trait_ontology_label` — currently only `opengwas-gwas-vcf-dense` (ukb-b) is
in this position; `gwas-ssf-ragged`'s GWAS Catalog source already supplies
`MAPPED_TRAIT`/`MAPPED_TRAIT_URI` for every currently-built bundle.

## Lookup contract

`mapping.tsv` has three columns: `trait_label`, `trait_ontology_id`,
`trait_ontology_label`. A generator looks up a row's own trait label against
`trait_label` using an **exact match** on the normalised (trimmed,
lowercased) string — no fuzzy or semantic matching. See
`resources/generators/lib/metadata_resolvers/canonical_trait_table.R`.

A miss is not an error: the resolver returns `trait_ontology_mapping_method =
unmapped` and leaves `trait_ontology_id`/`trait_ontology_label` blank, per
this registry's "never silently default" convention for resolvers (see
`resources/generators/lib/metadata_resolvers/contract.R` for the precedent this
follows).

## Why this table is empty today

Exact-match lookup only works when trait labels are already reasonably
clean and consistent. `gwas-ssf-ragged`'s GWAS Catalog-derived labels rarely
need this fallback (0 unmapped rows across every currently-built bundle at
time of writing). `opengwas-gwas-vcf-dense` (ukb-b)'s ~2,500 free-text UK
Biobank field descriptions (e.g. `"Operative procedures - secondary OPCS:
Z84.6 Knee joint"`) are exactly the opposite case: too numerous and too messy
for hand curation at this granularity, and exact string matching against them
would have a very low hit rate. Populating ukb-b-scale coverage needs real
candidate-generation-and-review tooling (see
`docs/adr/0021-trait-ontology-mapping-lookup-lives-in-registry.md`), tracked
as separate, larger follow-up work — not fabricated rows here.

Add a row only when a real, currently-unmapped trait needs one.
