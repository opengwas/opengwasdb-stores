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
| `chooser_id` | Identifier of the chooser — the model, tool, or process — that proposed the term. A row promoted by `curation.promotion` records the chooser's exact version in this cell as `<chooser_id>:<chooser_version>` (for example `stub:1`), because the ten-column header has no separate `chooser_version` column and the version must not be lost. |
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

## Where the rows come from

Exact-match lookup only works when trait labels are already reasonably
clean and consistent. `gwas-ssf-ragged`'s GWAS Catalog-derived labels rarely
need this fallback (0 unmapped rows across every currently-built bundle at
time of writing). `opengwas-gwas-vcf-dense` (ukb-b)'s ~2,500 free-text UK
Biobank field descriptions (e.g. `"Operative procedures - secondary OPCS:
Z84.6 Knee joint"`) are the opposite case: too numerous and too messy for
hand curation. Those rows come from the curation pipeline described in "How
rows are added".

The first round, `ukb-b-2026q3` (EFO v3.94.0, BioLORD-2023 retrieval, Jev
`jev-1.13.0`), auto-accepted 409 rows. It queued 1,056 labels for review and
recorded 1,037 as having no suitable EFO term. The validation and the
per-label outcomes are in `docs/trait-mapping-report.html`.

Add a hand-curated row only when a real, currently-unmapped trait needs one.
Fill in the provenance columns when a candidate-generation-and-review process
produced the row; leave them empty for a hand-curated row.

## How rows are added

Rows reach this table by two routes:

1. **Hand curation.** A curator adds a row directly, leaving the provenance
   columns empty or filling them in by hand (`review_status = human_reviewed`).
2. **The curation pipeline** (issue #161): `curation.gap_scan` derives the
   unmapped-Trait work queue, `curation.candidates` builds a lexical shortlist,
   `curation.choice` records a proposal, and `curation.promotion` gates that
   proposal on confidence and runner-up margin. A proposal that clears both
   thresholds is appended to this table with `review_status = auto_accepted`
   and a `reviewed_at` date, and the resource's integer `version` in
   `resource.yaml` is bumped. A proposal below either threshold goes to a
   review queue instead; the candidate shortlist (`--shortlists`) is required
   whenever anything is queued, so every review entry carries its full
   candidate shortlist and evidence (label, definition, parent term, channels,
   and channel ranks).

A rejected `(trait_label, ontology_id)` pair is retained in a rejection
registry and suppressed by every later promotion run, so a term a curator has
rejected is never re-proposed or auto-accepted.

A `none_suitable` proposal is never written to this table. When the chooser is
confident that no retrieved candidate denotes the trait, the label is recorded
in the round's `no-suitable-term.tsv` as unmapped by design rather than being
forced onto an approximate term; when the abstention is uncertain it goes to
the review queue, where a curator may amend in a real candidate. The curation
round's resumable stages (`curation.round`) keep every result file, the cost
ledger, and the per-bucket reconciliation in the round directory, so a rerun
resumes rather than repeating finished work.

A queued row can be reviewed in place: a curator sets `review_decision` to
`accept` (promote the proposal's selection) or `amend` (promote
`override_ontology_id`/`override_ontology_label`), with `curator` and
`curated_at`. The next promotion run reads those decisions back from the queue
itself (or from `--reviewed-queue`), appends the rows as
`review_status = human_reviewed`, bumps the resource `version`, and preserves
the decided rows -- including `reject` decisions -- in the rewritten queue.

## Review queues

Each curation round's review queue is committed here as
`review-queue-<round_id>.tsv`, for example `review-queue-ukb-b-2026q3.tsv`.
A queue holds every label the round did not auto-accept:

- an uncertain pick (`review_reason` `below_confidence` or `below_margin`);
- an uncertain abstention (`selected_ontology_id` `none_suitable`).

Each row carries the chooser's probabilities and its full candidate shortlist
(`candidates`, a JSON list with definitions and parent terms).

Confident abstentions are not queued. These are labels where the chooser was
confident that no shortlisted term denotes the trait. They are committed
separately as `no-suitable-term-<round_id>.tsv`, with the chooser's
probabilities and its best real candidate (`runner_up_id`,
`runner_up_label`). They are unmapped by design and no tool reads them back.
Some are candidates for new EFO terms, such as individual foods, keratometry
fields, and hospital administrative fields. To map one anyway, add a
hand-curated row to `mapping.tsv`.

To review, fill in `review_decision` (`accept`, `amend` or `reject`) and, for
`amend`, `override_ontology_id` and `override_ontology_label`. Also fill in
`curator` and `curated_at`, and commit the edited queue. Then apply the
decisions by passing the committed queue as **`--reviewed-queue`**, with a
scratch `--review-queue`:

```sh
# an empty proposals table: apply the committed decisions only
pixi run -e curation python -c "from curation.choice import PROPOSAL_COLUMNS; print(*PROPOSAL_COLUMNS, sep='\t')" > /tmp/no-proposals.tsv
pixi run -e curation python -m curation.promotion \
    --proposals /tmp/no-proposals.tsv \
    --reviewed-queue resources/reference-resources/canonical-trait-mapping-efo/review-queue-ukb-b-2026q3.tsv \
    --review-queue /tmp/review-queue-scratch.tsv \
    --resource-dir resources/reference-resources/canonical-trait-mapping-efo
```

`accept` and `amend` rows are appended to `mapping.tsv` as
`review_status = human_reviewed`. `reject` pairs are suppressed in every later
run. Rerunning is idempotent: a label already in the table is not appended
again.

Do **not** pass the committed queue as `--review-queue`. That file is
rewritten from the decided rows plus the rows re-queued from `--proposals`.
Without that round's proposals, every undecided row would be dropped.

