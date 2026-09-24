# Canonical trait mapping (EFO)

A curated fallback for Trait Ontology Mapping (see `CONTEXT.md`) when a
Source Collection doesn't supply its own `trait_ontology_id`/
`trait_ontology_label` — currently only `opengwas-gwas-vcf-dense` (ukb-b) is
in this position; `gwas-ssf-ragged`'s GWAS Catalog source already supplies
`MAPPED_TRAIT`/`MAPPED_TRAIT_URI` for every currently-built bundle.

## Lookup contract

The first three columns of `mapping.tsv` are the lookup columns and are the
only ones the resolver reads: `trait_label`, `trait_ontology_id`,
`trait_ontology_label`. A generator looks up a row's own trait label against
`trait_label` using an **exact match** on the normalised (trimmed,
lowercased) string — no fuzzy or semantic matching. See
`resources/generators/lib/metadata_resolvers/canonical_trait_table.R`.

`trait_ontology_label` is the human-readable label of the ontology term that
`trait_ontology_id` identifies — for example `carnitine measurement`
(`EFO:0010469`) or `multiple sclerosis` (`MONDO:0005301`), not the vocabulary
or identifier-authority name (`EFO`, `MONDO`, `Ensembl`). This is the same
semantics the committed Release Manifests use (see
`docs/release-metadata-schema.md`, "Column reference").

A miss is not an error: the resolver returns `trait_ontology_mapping_method =
unmapped` and leaves `trait_ontology_id`/`trait_ontology_label` blank, per
this registry's "never silently default" convention for resolvers (see
`resources/generators/lib/metadata_resolvers/contract.R` for the precedent this
follows).

## Provenance columns

Beyond the three lookup columns, `mapping.tsv` carries columns recording how
each row was chosen, so a reviewer can audit a mapping without re-running the
candidate-generation step:

| Column | Meaning |
|---|---|
| `ontology_release` | Release/version of the ontology the chosen term was read from (for example an EFO or MONDO release identifier). |
| `chooser_id` | Identifier of the chooser — the model, tool, or process — that proposed the term. |
| `confidence` | The chooser's own confidence in the chosen term, as a number between 0 and 1. |
| `runner_up_margin` | Difference in score between the chosen term and the next-best candidate; small margins flag rows worth a closer look. |
| `review_status` | Controlled value: `auto_accepted` (accepted on the chooser's output without a human) or `human_reviewed` (a person checked and accepted it). |
| `reviewer` | Who reviewed the row, when `review_status = human_reviewed`. |
| `reviewed_at` | Date the row was reviewed, when `review_status = human_reviewed`. |

Provenance columns are **optional**. A hand-curated row may leave every one of
them empty (or omit them entirely, as the earliest committed rows did) and
still resolve normally; they exist to carry evidence, not to gate lookup. The
resolver ignores any column it does not know, so widening this table never
changes lookup behaviour — it only adds provenance for reviewers.

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

Add a row only when a real, currently-unmapped trait needs one. Fill in the
provenance columns when a candidate-generation-and-review process produced the
row; leave them empty for a hand-curated row.
