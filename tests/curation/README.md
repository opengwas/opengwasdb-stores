# Curation test suites

Hermetic, network-free test suites for the Canonical Trait Mapping Table
curation pipeline (issue #161):

- `test_gap_scan.py` — the unmapped Trait work queue (issue #163);
- `test_candidates.py` — pinned ontology release and lexical candidate
  generation (issue #164);
- `test_embedding.py` — the semantic embedding channel: hermetic fixture
  vectors, channel attribution, model/index pins, and clean degradation
  (issue #166);
- `test_harvest.py` — the source-provided validation set (issue #165);
- `test_recall.py` — stratified retrieval recall, the ukb-b stratum gap, and
  the semantic channel's incremental delta (issues #165/#166);
- `test_choice.py` — the chooser interface, the fixture-backed stub chooser,
  and the proposals table (issue #167);
- `test_promotion.py` — the confidence/margin gate, the review queue, rejection
  persistence, and the Reference Resource version bump (issue #169);
- `test_jev_chooser.py` — the Jev-backed structured-decision chooser: the
  255-option cap, byte/token budgets, enum-constrained wire payload, unmodified
  probabilities, and the hosted/fixture clients (issue #168);
- `test_validate_chooser.py` — choice accuracy conditional on retrieval, the
  reliability curve, stratified reporting, caveats, and cost tracking
  (issue #168);
- `test_e2e_pipeline.py` — the full pipeline round trip from `analyses.tsv` to
  a promoted Canonical Trait Mapping Table row and the real R resolver
  (issue #168);
- `test_coverage.py` — the round coverage report and the end-to-end round
  runner: before/after unmapped rates per Store Family in Analyses resolved,
  rows added kept distinct, review queue size, no-candidate labels left
  unmapped, cost accounting, and the strict no-Manifest/no-bundle/no-store
  boundary (issue #170).

## Gap-scan suite (`curation.gap_scan`, issue #163)

### The contract this suite exists for

Trait Ontology Mapping is frozen into every Release Manifest's `analyses.tsv`.
Rows with `trait_ontology_mapping_method = unmapped` are the curation gap. The
gap scan must turn an explicit set of committed Manifests into a correct,
prioritised work queue for Canonical Trait Mapping Table curation, without
modifying a Manifest, a generator, or a bundle.

A wrong queue is silently expensive: a mis-normalised label fragments one
curation candidate into several, an under-count demotes a high-value label, and
a wrong Store Family attribution sends the review to the wrong place.

### Contracts and invariants covered

1. **Selection**: only `unmapped` rows are queued; `source_provided` and
   `canonical_table_lookup` rows are excluded.
2. **Normalisation**: labels are trimmed then lowercased, exactly the R
   resolver's `trimws(tolower(x))`; case/whitespace variants collapse and their
   occurrence counts sum.
3. **Attribution**: `store_families` is derived from a sibling `release.yaml`
   (`store_family_id`/`family`), a `releases/` or `families/` path segment, or
   the bundle directory name, and is rendered sorted and comma-separated.
4. **Ordering**: the queue is descending by `occurrence_count` with an
   alphabetical tie-break, so output is deterministic.
5. **Empty is not an error**: a Manifest with no unmapped rows, or with no
   mapping column at all, contributes nothing and the CLI exits 0.
6. **Loud failure**: a missing path, a ragged row, and a Manifest with a mapping
   column but no recognisable trait label raise; the CLI reports and exits 1.
7. **CLI surface**: manifest files and bundle directories are accepted; output
   goes to stdout or `--output`; the header is always present.

## Candidate-generation suite (`curation.candidates`, issue #164)

### The contract this suite exists for

Candidate generation turns each queued label into a shortlist of plausible
ontology terms using lexical channels only — no model and no network. It
resolves against a rebuildable retrieval index derived from a pinned ontology
release, and every shortlist row carries that release string so a later
proposal can state what it was resolved against. Retrieval is the ceiling on
the whole pipeline: a term not retrieved here can never be chosen, and a
fabricated candidate can reach a Release Manifest.

### Contracts and invariants covered

1. **Channels**: exact, normalised, token-overlap, and synonym/acronym channels
   each retrieve their intended term.
2. **Attribution**: a candidate records which channels found it and each
   channel's rank; candidates found by several channels are deduplicated by
   ontology id.
3. **Evidence**: a candidate carries the term's label, definition, parent id,
   and parent label.
4. **Pin**: the pinned ontology release travels on every candidate and every
   output row.
5. **Bounded shortlist**: the shortlist size is configurable and respected; a
   label no channel matches yields an empty shortlist, never a fabricated term.
6. **Obsolete terms**: obsolete terms are flagged in the shortlist rather than
   silently offered as live terms.
7. **Index**: the retrieval index round-trips through its rebuildable artifact,
   rejects an unknown format version, and defaults outside the tracked tree
   (`.cache/curation/`).
8. **CLI surface**: the command reads the gap-scan work queue, resolves against
   the index, and writes the shortlist table to `--output` or stdout.

The suite resolves against a tiny in-memory fixture OBO document, never a real
release, so it is hermetic.

## Embedding suite (`curation.embedding`, issue #166)

### The contract this suite exists for

Lexical channels cannot reach a Trait label that shares no token with the
correct ontology term. The semantic channel embeds each term's label,
synonyms, and definition into a nearest-neighbour index and contributes
candidates under exactly the same attribution rules as the lexical channels.
It is individually enableable and must degrade to lexical-only when its index
or model is unavailable, so no run that worked before stops working.

### Contracts and invariants covered

1. **Reach**: the channel retrieves a term sharing no lexical overlap with the
   query, where every lexical channel returns nothing.
2. **Indexed text**: a term is indexed by its label, its synonyms, and its
   definition, not by its label alone.
3. **Attribution**: a semantic candidate records the `embedding` channel and
   its rank like any other channel, and the shortlist row records the pinned
   model id and the content-addressed index build alongside the ontology
   release. Provenance is recorded only when semantic retrieval actually ran
   for the label; a disabled or degraded channel leaves it empty.
4. **Pins**: the embedding index round-trips, rejects an unknown format
   version, and records its model, release, and build metadata; the build id
   is content-addressed, so a changed vector changes it. Loading also rejects
   an artifact whose declared dimension or build id disagrees with its vectors.
5. **Degradation**: no retriever leaves the shortlist lexical-only; a missing,
   stale, release-mismatched, or model-mismatched index, and an embedder that
   fails at query time, all contribute nothing rather than raising. An
   endpoint/connection failure also trips a run-level circuit breaker, so the
   channel is not retried (and does not time out) for every later label.
6. **CLI surface**: `--enable-embedding` / `--embedding-index` enable the
   channel, and an unavailable index prints a warning and leaves the run
   lexical-only with exit 0.

The suite is hermetic: genuine semantic retrieval is exercised with
`DictionaryEmbedder` replaying explicit dense fixture vectors (and the offline
`HashingEmbedder` for token-based runs), while the `HttpEmbedder` response
validation is exercised through an injected fake client. Nothing opens a
socket.

## Harvest suite (`curation.harvest`, issue #165)

### The contract this suite exists for

Rows whose `trait_ontology_mapping_method` is `source_provided` carry the
Source Collection's own ontology term for a Trait, making them ground truth for
retrieval. The harvest must turn an explicit set of committed Manifests into a
correct validation set, without modifying a Manifest, a generator, or a bundle.

### Contracts and invariants covered

1. **Selection**: only `source_provided` rows are harvested; `unmapped` and
   `canonical_table_lookup` rows are excluded.
2. **Uniqueness**: pairs are unique on
   `(trait_label, ontology_id, ontology_label, stratum)` and Store Families are
   unioned across occurrences.
3. **Stratum**: categorisation follows the documented precedence — MONDO is
   disease, OBA is measurement, an analyte/measurement Store Family is a
   measurement, a measurement label is a measurement, a disease family is
   disease, else `other`.
4. **Obsolete**: a term found in the pinned index reports its own obsolete
   flag; an absent term is never assumed obsolete and only the explicit
   `obsolete_` source-label convention flags it.
5. **CLI surface**: manifest files and bundle directories are accepted; output
   goes to stdout or `--output`; the header is always present.

## Recall suite (`curation.recall`, issue #165)

### The contract this suite exists for

Retrieval must be scored against the ground truth *blind* — shortlists are
generated from the Trait label alone, never from the known ontology id. The
report must break recall down by stratum and shortlist size, exclude obsolete
terms, enumerate misses, and always state the ukb-b stratum-gap caveat.

### Contracts and invariants covered

1. **Blind scoring**: a pair whose label matches nothing does not retrieve its
   own target id, even when that id is in the index.
2. **Sizes**: a correct id inside the shortlist is a hit at every size at or
   above its rank, and a miss below it; recall is computed per stratum and in
   aggregate.
3. **Obsolete**: obsolete pairs are excluded from scoring and counted.
4. **Misses**: misses are enumerated per size with the rank at which the id was
   found, so near-misses are inspectable.
5. **Disclaimer**: every format (text, markdown, tsv) states plainly that no
   validation stratum matches the ukb-b label distribution of disease,
   procedure, and administrative free-text.
6. **Semantic delta**: the same validation set scored lexical-only and with the
   semantic channel enabled yields a per-stratum delta at every size; a
   negative delta is reported as-is, and the delta appears in every format.
7. **CLI surface**: the command scores against an index or pre-generated
   shortlists and writes the report to stdout or `--output`; the semantic
   delta requires `--index` and degrades to the baseline with a warning when
   the channel is unavailable.

Both suites resolve against tiny in-memory fixtures, never a real release, so
they are hermetic.

## Choice-stage suite (`curation.chooser`, `curation.stub_chooser`,
`curation.choice`, issue #167)

### The contract this suite exists for

Candidate generation is the ceiling on the whole pipeline: a term it did not
retrieve can never be mapped. The choice stage runs a chooser over each
shortlist and records one proposal per trait label. The structural rule is that
a chooser may only select from the shortlist it was handed — it can never
invent a term — and that an empty shortlist yields no proposal rather than an
arbitrary selection.

The suite is hermetic: the only chooser exercised is the fixture-backed
`StubChooser`, so there is no model, no network, and no non-determinism.

### Contracts and invariants covered

1. **Interface**: `Chooser.choose` returns the explicit no-proposal outcome for
   an empty shortlist, and validates that the selection and the distribution
   cover exactly the shortlist.
2. **Hard error**: a selection outside the shortlist raises
   `SelectionNotInShortlistError`; the CLI reports it and writes no proposal.
3. **Stub chooser**: recorded selections and distributions replay
   deterministically from a mapping, JSON, or TSV fixture; an unrecorded trait
   label fails loudly; unmentioned candidates receive probability 0.0.
4. **Arithmetic**: the winner, runner-up, confidence, and runner-up margin
   (`1.0` for a single candidate) are calculated from the distribution, with a
   deterministic shortlist-order tie-break.
5. **Proposal table**: the documented columns are emitted, carrying
   `chooser_id`, `chooser_version`, and the pinned `ontology_release`.
6. **CLI surface**: the command reads a shortlist, runs the stub chooser from a
   fixture, and writes the table to `--output` or stdout.

## Promotion suite (`curation.promotion`, issue #169)

### The contract this suite exists for

The promotion stage is the only place a proposal becomes a committed Canonical
Trait Mapping Table row, and it must never do so on evidence too weak for a
human to have skipped. A proposal is auto-accepted only when *both* its
confidence and its runner-up margin clear configurable thresholds; everything
else goes to a review queue a curator works from. A previously rejected
`(trait_label, ontology_id)` pair is suppressed on every later run, and a new
promotion bumps the Reference Resource's integer `version`.

### Contracts and invariants covered

1. **Gating**: eligibility is an inclusive AND over `confidence` and
   `runner_up_margin`; a high-confidence winner over a near-tie is queued, not
   promoted. `confidence`, `runner_up_margin`, `runner_up_confidence`, and
   every probability must be finite and in `[0, 1]`: a `NaN` would otherwise
   compare false against every bound and be auto-accepted.
2. **Promotion**: eligible rows are appended to `mapping.tsv` with the issue
   #162 provenance columns, `review_status = auto_accepted`, and an ISO
   `reviewed_at`; the three resolver lookup columns are first, and the exact
   chooser version is preserved in the `chooser_id` cell as
   `<chooser_id>:<chooser_version>`.
3. **Review queue**: sub-threshold proposals carry the proposal, their full
   candidate shortlist and evidence as JSON (every shortlisted candidate, with
   definition, parent term, channels, and channel ranks -- never a blank
   label), and empty `review_decision`, `override_ontology_id`,
   `override_ontology_label`, `curator_notes`, `curator`, and `curated_at`
   columns. A queued proposal without a shortlist raises instead of writing an
   entry with missing evidence.
4. **Human review round-trip**: a review queue edited in place is read back on
   the next run (or via `--reviewed-queue`). `accept` promotes the proposal's
   own selection and `amend` promotes the override, both as
   `review_status = human_reviewed` with the curator and date and a resource
   `version` bump; `reject` suppresses the pair. Decided rows -- including
   rejections -- are preserved in the rewritten queue, never discarded.
5. **Rejections**: a rejection registry (or a reviewed queue whose decision is
   `reject`/`amend`) suppresses the pair; the registry is read, never
   rewritten, and suppression persists across runs.
6. **Version bump**: promoting at least one new row increments the integer
   `version` in `resource.yaml` by exactly one; a re-run that promotes nothing
   leaves both the table and the version untouched.
7. **Strict boundaries**: the only files promotion writes are the Reference
   Resource directory's `mapping.tsv`/`resource.yaml` and the review queue
   file; no Release Manifest, bundle, or store is modified.
8. **Resolver**: promoted rows resolve through
   `resolve_trait_ontology_mapping()` as `canonical_table_lookup`.
9. **CLI surface**: the command reads the proposals table, applies the
   thresholds and any curator decisions, and writes the two outputs.

The suite is hermetic: it runs against temporary fixture tables and invokes
the real R resolver only against its own fixture, so it never touches the real
curated data.

## Jev chooser, validation, and end-to-end suite (`curation.jev_chooser`,
`curation.validate_chooser`, issue #168)

### The contract these suites exist for

`curation.stub_chooser` proves the choice stage can be wired together; the Jev
chooser is the live implementation, and the validation runner is how its
accuracy is measured. The Jev chooser must be structurally incapable of
proposing a term outside the shortlist: its request exposes the shortlist as a
TypeSafe JSON enum, so a conforming model can only return one of those ids. It
also enforces Jev's hard limits -- 255 enum options and an input byte/token
budget -- at configuration time, before a request is sent.

Choice accuracy must be measured *conditional on retrieval*. Folding retrieval
misses into the chooser's score would blame the chooser for a term candidate
generation never found, so the validation runner excludes those pairs and
counts them separately.

### Contracts and invariants covered

1. **Option cap**: a shortlist larger than `MAX_JEV_OPTIONS` (255) raises
   `JevConfigurationError` before any client call; exactly 255 is accepted.
2. **Budgets**: a payload over `max_input_bytes` or the estimated
   `max_input_tokens` raises at configuration time with an actionable message.
3. **Enum payload**: the request's response schema exposes exactly the
   shortlist's ontology ids as the enum (and as the probability property keys),
   so the model cannot invent a term.
4. **Unmodified probabilities**: the calibrated distribution reaches
   `ChoiceResult` unchanged; the selection is the model's explicit choice or
   the argmax (shortlist order breaks ties), and the base class rejects a
   selection that contradicts it.
5. **Clients**: the hosted `HttpJevClient` is exercised through an injected
   fake transport (never a socket) and the `FixtureJevClient` replays recorded
   decisions from a mapping, JSON, or TSV fixture; an unrecorded label fails
   loudly.
6. **Cost**: a response that reports `cost_usd` is recorded per label, and the
   chooser exposes the per-label records and total spend.
7. **Conditional accuracy**: pairs whose correct term is not in the shortlist
   are excluded from the chooser's score and counted as retrieval misses;
   accuracy is reported per stratum (analyte measurement vs disease, plus
   `other`) and in aggregate.
8. **Reliability curve**: reported probabilities are binned against observed
   accuracy, including empty bins.
9. **Caveats and recommendation**: the report states the analyte-measurement
   dominance and whether the disease stratum is large enough to be evidence,
   and recommends a promotion confidence threshold and runner-up margin from
   the observed curve.
10. **Explicit live runs**: a hosted Jev validation requires `--live`; the
    harness never opens a socket otherwise, and it is not registered as a CI
    suite.
11. **End-to-end round trip**: `analyses.tsv` -> `gap_scan` -> `candidates` ->
    `choice` (stub) -> `promotion` produces a Canonical Trait Mapping Table row
    that the real R `resolve_trait_ontology_mapping()` resolves with
    `resolution_status = "resolved"`,
    `trait_ontology_mapping_method = "canonical_table_lookup"`, and the correct
    ontology id and label.

All three suites are hermetic: tiny in-memory OBO/index fixtures, the fixture
choosers, and (for the round trip) the repository's own R resolver against its
own temporary table. Nothing opens a socket.

## Coverage and round-runner suite (`curation.coverage`,
`curation.curation_round`, issue #170)

### The contract this suite exists for

A curation round must be reported in the language of the corpus, not the
machinery. A promoted Canonical Trait Mapping Table row maps one *trait label*,
but that label can appear on many Analyses across several Store Families, so
the headline figure is **Analyses resolved**, and it must never be conflated
with the number of rows appended. The report must also state the work the
round leaves behind -- the review queue and the labels for which no candidate
was ever retrieved -- so the residual unmapped rate is not mistaken for a
failure.

### Contracts and invariants covered

1. **Analyses resolved vs rows added**: one promoted row resolving many
   Analyses across several families reports one row added and the full
   Analyses-resolved count; the two fields are separate.
2. **Before/after per Store Family**: each family's before rate is its
   committed unmapped Analyses over its total Analyses; its after rate subtracts
   exactly the Analyses whose label the round promoted. A promoted label absent
   from a family resolves nothing there.
3. **Normalisation**: labels differing by case or surrounding whitespace
   collapse, and a duplicated or unnormalised promoted label counts one row.
4. **Review queue size**: only labels still awaiting a human curator are
   counted; a decided row is not awaiting anyone.
5. **No candidates**: labels in the work queue with no shortlist row are
   counted, and a full round leaves them unmapped rather than forcing an
   approximate term.
6. **Cost**: a cost-reporting chooser's per-label spend is summed into the
   round total; an offline chooser is reported as untracked, not free.
7. **Formats**: text, Markdown, and TSV carry the same before/after rates,
   Analyses resolved, rows added, review queue size, no-candidate count, and
   total cost; the TSV carries the globals as `#` comment lines.
8. **Strict boundaries**: a round writes only the Reference Resource directory
   and its work directory; no Release Manifest, accepted bundle, or built
   Store Release is modified, and `--dry-run` leaves even the real mapping
   table untouched.

The suite is hermetic: tiny in-memory OBO/index fixtures, the stub chooser, and
temporary Reference Resource copies. Nothing opens a socket.

## Running the suites

```sh
pixi run python tests/curation/test_gap_scan.py
pixi run python tests/curation/test_candidates.py
pixi run python tests/curation/test_embedding.py
pixi run python tests/curation/test_harvest.py
pixi run python tests/curation/test_recall.py
pixi run python tests/curation/test_choice.py
pixi run python tests/curation/test_promotion.py
pixi run python tests/curation/test_jev_chooser.py
pixi run python tests/curation/test_validate_chooser.py
pixi run python tests/curation/test_e2e_pipeline.py
pixi run python tests/curation/test_coverage.py
# or through the repo orchestrator
pixi run test-python
```

To measure the semantic channel's delta over the lexical-only baseline (the
recall report re-run required by issue #166):

```sh
# build the pinned embedding index from the lexical retrieval index
pixi run -e curation embedding-index \
    --ontology-index .cache/curation/efo-v3.78.0.index.json \
    --output .cache/curation/efo-v3.78.0--local-hashing-v1.embedding.json \
    --model local-hashing-v1

# score the same validation set lexical-only and with the channel enabled
pixi run -e curation recall \
    --validation <validation.tsv> \
    --index .cache/curation/efo-v3.78.0.index.json \
    --enable-embedding \
    --embedding-index .cache/curation/efo-v3.78.0--local-hashing-v1.embedding.json
```

The `local-hashing-v1` model is the offline embedder; a hosted model such as
the pinned `all-MiniLM-L6-v2` needs `--embedding-endpoint` (or
`OPENGWASDB_EMBEDDING_ENDPOINT`). An unavailable channel prints a warning and
the report stays lexical-only.

To validate a chooser's choice accuracy conditional on retrieval (issue #168):

```sh
# offline: replay a fixture chooser (this is what CI never runs)
pixi run -e curation validate-chooser \
    --validation <validation.tsv> \
    --shortlists <shortlist.tsv> \
    --chooser stub --fixture <fixture.json>

# live: explicitly opt in to the hosted Jev service
pixi run -e curation validate-chooser \
    --validation <validation.tsv> \
    --shortlists <shortlist.tsv> \
    --chooser jev --jev-endpoint "$OPENGWASDB_JEV_ENDPOINT" --live
```

The validation set is the harvest of `source_provided` rows from
`gwas-ssf-ragged` (and hybrid) manifests; the shortlists are the candidate
tables for those labels. The report separates retrieval misses from choice
errors, bins reported probability against observed accuracy, and recommends a
promotion confidence and runner-up margin threshold. `--live` is required for a
hosted endpoint so the harness never opens a socket by accident.

To run a full curation round and report its coverage (issue #170):

```sh
# rehearse the round against a copied Reference Resource; nothing real is written
pixi run -e curation curation-round \
    --index .cache/curation/efo-v3.78.0.index.json \
    --chooser stub --fixture <fixture.json> \
    --dry-run

# live round: the confident proposals are promoted and the resource version bumped
pixi run -e curation curation-round \
    --index .cache/curation/efo-v3.78.0.index.json \
    --chooser jev --jev-endpoint "$OPENGWASDB_JEV_ENDPOINT"

# report coverage from an existing round's artifacts (read-only)
pixi run -e curation coverage \
    --manifests families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv \
    --mapping resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv \
    --work-queue .cache/curation/round/work-queue.tsv \
    --shortlists .cache/curation/round/shortlists.tsv \
    --review-queue .cache/curation/round/review-queue.tsv \
    --format markdown
```

The round defaults to the committed ukb-b Dense observed VCF manifest. It
promotes a proposal only when its confidence is at least `0.85` *and* its
runner-up margin is at least `0.20`; everything else is queued for a human
curator, and a label with no retrieved candidate is left unmapped by design.
The coverage report states the before/after unmapped rate per Store Family in
Analyses resolved, the rows added, the review queue size, the no-candidate
count, and the total round cost.
