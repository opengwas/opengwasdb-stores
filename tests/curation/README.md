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
  254-candidate cap plus the `none_suitable` abstention, byte/token budgets,
  the real TypeSafe criteria payload and response parsing (model/tokens/cost),
  unmodified probabilities, retries and key resolution, and the hosted/fixture
  clients (issue #168);
- `test_validate_chooser.py` — choice accuracy conditional on retrieval, the
  reliability curve, stratified reporting, caveats, cost tracking, and
  abstention handling (issue #168);
- `test_e2e_pipeline.py` — the full pipeline round trip from `analyses.tsv` to
  a promoted Canonical Trait Mapping Table row and the real R resolver
  (issue #168);
- `test_coverage.py` — the round coverage report and the end-to-end round
  runner: before/after unmapped rates per Store Family in Analyses resolved,
  rows added kept distinct, review queue size, no-candidate labels left
  unmapped, cost accounting, and the strict no-Manifest/no-bundle/no-store
  boundary (issue #170).
- `test_round.py` — the resumable round stages: per-trait atomic result files,
  skip-on-identical-fingerprint and rerun-on-changed-shortlist, error files and
  retry, interrupt safety, `--max-cost-usd`, bucket reconciliation including
  stale results, `none_suitable` routing, coverage from the full table, and
  `round.yaml` pin-mismatch refusal (issue #161).

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
8. **Scale and equivalence**: the channels are served by lookup structures
   built once per loaded index (exact/normalised label maps, token postings,
   synonym and acronym maps) and their ids, order, and ranks are identical to
   the brute-force scans they replace, over both the fixture index and a
   randomised synthetic index. The full ukb-b queue (2,502 labels) against the
   real v3.94.0 index runs lexical-only in well under a minute.
9. **CLI surface**: the command reads the gap-scan work queue, resolves against
   the index, and writes the shortlist table to `--output` or stdout;
   `--ontology-embeddings` / `--trait-embeddings` add the semantic channel.

The suite resolves against a tiny in-memory fixture OBO document, never a real
release, so it is hermetic.

## Embedding suite (`curation.embedding`, issues #166, #161)

### The contract this suite exists for

Lexical channels cannot reach a Trait label that shares no token with the
correct ontology term. The semantic channel compares a query vector for the
label against a vector store of each term's label, synonyms, and definition
and contributes candidates under exactly the same attribution rules as the
lexical channels. Ontology vectors live in a numpy directory store, and query
vectors come from a precomputed trait store (no network) when one covers the
label, or from the pinned hosted model when an endpoint is configured. The
channel is individually enableable and must degrade to lexical-only when its
store or model is unavailable, so no run that worked before stops working.

### Contracts and invariants covered

1. **Reach**: the channel retrieves a term sharing no lexical overlap with the
   query, where every lexical channel returns nothing.
2. **Indexed text**: a term is indexed by its label, its synonyms, and its
   definition, not by its label alone; a trait store is keyed by the
   normalised trait label.
3. **Attribution**: a semantic candidate records the `embedding` channel and
   its rank like any other channel, and the shortlist row records the pinned
   model id and the content-addressed ontology store build alongside the
   ontology release. Provenance is recorded only when semantic retrieval
   actually ran for the label; a disabled or degraded channel leaves it empty,
   and a label with no precomputed vector and no endpoint is counted as
   lexical-only.
4. **Vector store**: the directory artifact (`vectors.npy` float32
   L2-normalised, `ids.tsv`, `meta.yaml`) round-trips; nearest-neighbour is one
   matrix multiply plus `argpartition` per query batch; the loader rejects a
   wrong format version, a count/dimension mismatch, or a build id that
   disagrees with the vector bytes; and resolution refuses an ontology store
   and a trait store whose model, release, or dimension disagree.
5. **Resumable builds**: `embed-ontology` and `embed-traits` write each
   finished chunk under `<store>/chunks/` and an interrupted run resumes,
   requesting only the chunks it is missing.
6. **Hosted client**: the OpenAI-compatible `/v1/embeddings` client batches at
   128 texts by default, retries 429/5xx/transport errors with exponential
   backoff, and refuses vectors returned for a different model.
7. **Degradation**: no retriever leaves the shortlist lexical-only; a missing,
   stale, release-mismatched, or model-mismatched store, and an embedder that
   fails at query time, all contribute nothing rather than raising. An
   endpoint/connection failure also trips a run-level circuit breaker, so the
   channel is not retried (and does not time out) for every later label.
8. **CLI surface**: `--enable-embedding` / `--ontology-embeddings` /
   `--trait-embeddings` enable the channel, and an unavailable store prints a
   warning and leaves the run lexical-only with exit 0.

The suite is hermetic: genuine nearest-neighbour retrieval is exercised with
`DictionaryEmbedder` replaying explicit dense fixture vectors (and the offline
`HashingEmbedder` *stub*, which is plumbing, not a semantic model), while the
`HttpEmbedder` retry/backoff and response validation are exercised through an
injected fake client. Nothing opens a socket.

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
   cover exactly the shortlist (plus an optional `none_suitable` abstention).
2. **Hard error**: a selection outside the shortlist raises
   `SelectionNotInShortlistError`; the CLI reports it and writes no proposal.
3. **Abstention**: a chooser may select `NONE_SUITABLE`, meaning no retrieved
   candidate denotes the trait. Its probability may appear alongside the
   shortlist, and the outcome becomes a proposals row with
   `selected_ontology_id = none_suitable` and an empty label rather than being
   dropped.
4. **Stub chooser**: recorded selections and distributions replay
   deterministically from a mapping, JSON, or TSV fixture; an unrecorded trait
   label fails loudly; unmentioned candidates receive probability 0.0; a
   fixture may select the abstention.
5. **Arithmetic**: the winner, runner-up, confidence, and runner-up margin
   (`1.0` for a single candidate) are calculated from the distribution, with a
   deterministic shortlist-order tie-break.
6. **Proposal table**: the documented columns are emitted, carrying
   `chooser_id`, `chooser_version`, and the pinned `ontology_release`.
7. **CLI surface**: the command reads a shortlist, runs the stub chooser from a
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
chooser is the live implementation against TypeSafe's `jev-1.13.0` model, and
the validation runner is how its accuracy is measured. The Jev chooser must be
structurally incapable of proposing a term outside the shortlist: its request
exposes the shortlist as a TypeSafe `choice` question's `criteria` map, so a
conforming model can only return one of those ids -- plus the reserved
`none_suitable` abstention. It also enforces Jev's limits -- 254 real
candidates plus the abstention (255 options) and an input byte/token budget --
at configuration time, before a request is sent. The live client retries
429/529 and 5xx/transport failures with exponential backoff honouring
`retry-after`, never retries 401/422, and resolves the API key from the
explicit argument, then `TYPESAFE_API_KEY`, then `key="..."` in `~/.typesafe`
without ever echoing it.

Choice accuracy must be measured *conditional on retrieval*. Folding retrieval
misses into the chooser's score would blame the chooser for a term candidate
generation never found, so the validation runner excludes those pairs and
counts them separately. A `none_suitable` selection is an abstention:
reported separately with a count and rate and excluded from both the accuracy
denominator and the reliability curve.

### Contracts and invariants covered

1. **Option cap**: a shortlist larger than 254 real candidates raises
   `JevConfigurationError` before any client call; 254 is accepted and the
   request carries 255 options including `none_suitable`.
2. **Budgets**: a payload over `max_input_bytes` or the estimated
   `max_input_tokens` raises at configuration time with an actionable message.
3. **Criteria payload**: the request's `questions.term.criteria` exposes exactly
   the shortlist's ontology ids plus `none_suitable`, with a compact
   deterministic criteria string per candidate (label, parent, truncated
   definition, obsolete flag), so the model cannot invent a term and an
   abstention is always available.
4. **Unmodified probabilities**: the calibrated distribution reaches
   `ChoiceResult` unchanged (plus the reserved abstention option); the
   selection is the model's explicit `choice` or the argmax (shortlist order
   breaks ties), and the base class rejects a selection that contradicts it.
5. **Provenance**: the answering `model`, the model's own `confidence`, input
   token usage, the derived cost
   (`input_tokens / 1e6 * price_per_mtok_input`, default `0.042`), the parsed
   response, and a deterministic request fingerprint reach `ChoiceResult`.
6. **Clients**: the hosted `HttpJevClient` is exercised through an injected
   fake transport and injected sleep (never a socket or a real wait) and the
   `FixtureJevClient` replays recorded decisions from a mapping, JSON, or TSV
   fixture; an unrecorded label fails loudly.
7. **Retries and key**: 429/529/5xx retry with exponential backoff honouring
   `retry-after`, 401/422 never retry, and the API key resolves in the
   documented order without appearing in any exception message.
8. **Conditional accuracy**: pairs whose correct term is not in the shortlist
   are excluded from the chooser's score and counted as retrieval misses;
   accuracy is reported per stratum (analyte measurement vs disease, plus
   `other`) and in aggregate.
9. **Reliability curve**: reported probabilities are binned against observed
   accuracy, including empty bins; abstentions are excluded.
10. **Caveats and recommendation**: the report states the analyte-measurement
    dominance, whether the disease stratum is large enough to be evidence, and
    the abstention count and rate, and recommends a promotion confidence
    threshold and runner-up margin from the observed curve.
11. **Explicit live runs**: a hosted Jev validation requires `--live`; the
    harness never opens a socket otherwise, and it is not registered as a CI
    suite.
12. **End-to-end round trip**: `analyses.tsv` -> `gap_scan` -> `candidates` ->
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

## Resumable round runbook (issue #161)

A real round over the ukb-b queue runs as independent, idempotent stages. Each
stage reads its pins from `<round-dir>/round.yaml` and refuses to run when the
ontology release, embedding model, or store build disagrees with them. The
round directory is gitignored (default `.cache/curation/rounds/<round-id>/`).

```sh
# 0. build the pinned lexical index (v3.94.0)
pixi run -e curation ontology-index \
    --obo <efo.obo> \
    --output .cache/curation/efo-v3.94.0.index.json

# 1. embed the ontology terms once (resumable under <store>/chunks/)
pixi run -e curation embed-ontology \
    --ontology-index .cache/curation/efo-v3.94.0.index.json \
    --output .cache/curation/efo-v3.94.0--BioLORD-2023.ontology-embeddings \
    --model FremyCompany/BioLORD-2023 \
    --endpoint "$OPENGWASDB_EMBEDDING_ENDPOINT"

# 2. pin the round: ontology index + ontology store + chooser + thresholds + inputs
#    (the trait store does not exist yet; it is built from the round's queue)
pixi run -e curation round-init \
    --round-dir .cache/curation/rounds/ukb-b-2026q1 \
    --index .cache/curation/efo-v3.94.0.index.json \
    --ontology-embeddings .cache/curation/efo-v3.94.0--BioLORD-2023.ontology-embeddings \
    --chooser jev \
    --manifests families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv \
    --threshold-evidence "conf 0.85 / margin 0.20 from the validation curve"

# 3. derive the queue, excluding labels already in the mapping table
pixi run -e curation round-gap-scan \
    --round-dir .cache/curation/rounds/ukb-b-2026q1

# 4. precompute the queue's trait vectors with the SAME model as the ontology
#    store and record the store pin in round.yaml (no --force needed)
pixi run -e curation round-embed-traits \
    --round-dir .cache/curation/rounds/ukb-b-2026q1 \
    --endpoint "$OPENGWASDB_EMBEDDING_ENDPOINT"

# 5. shortlist every queued label from the precomputed vectors (no network)
pixi run -e curation round-candidates \
    --round-dir .cache/curation/rounds/ukb-b-2026q1

# 6. map: one atomic result file per trait, concurrent and cost-capped
pixi run -e curation round-choose \
    --round-dir .cache/curation/rounds/ukb-b-2026q1 \
    --workers 8 --max-cost-usd 25

# 7. reduce: proposals, cost ledger, and a per-bucket reconciliation
pixi run -e curation round-reduce \
    --round-dir .cache/curation/rounds/ukb-b-2026q1

# 8. promote: confident rows only; abstentions and weak rows never reach it
pixi run -e curation round-promote \
    --round-dir .cache/curation/rounds/ukb-b-2026q1

# 9. report the after state from the whole post-promotion table
pixi run -e curation round-coverage \
    --round-dir .cache/curation/rounds/ukb-b-2026q1 --format markdown
```

`round-choose` writes `choices/<2hex>/<sha256>.yaml` atomically (temp + fsync
+ rename). A label whose result already carries the current request fingerprint
is skipped; a changed shortlist, model, or context changes the fingerprint and
forces a rerun. A failed request writes `<sha256>.error.yaml` (error class,
message, attempts, timestamp) and the stage continues; a later success removes
it. `--limit N` pilots the first N labels that still need work, and
`--max-cost-usd X` stops issuing new requests once the cumulative recorded cost
reaches `X`. Ctrl-C leaves only complete result files.

`round-reduce` classifies every queued label into exactly one bucket --
`no_candidate`, `pending` (no result, or a result for a stale shortlist),
`error`, `none_suitable`, or `proposed` -- and exits non-zero when any label is
`pending`/`error` unless `--allow-incomplete` is given. `round-promote` never
writes a `none_suitable` abstention to `mapping.tsv`: a confident abstention is
recorded in `no-suitable-term.tsv` (unmapped by design) and an uncertain one
goese to `review-queue.tsv`.

### The harvested-validation-set variant

Harvest `source_provided` rows and run the same machinery over them by pinning
the harvested validation set as an explicit queue instead of manifests:

```sh
pixi run -e curation harvest <manifest>... --index <index.json> --output <validation.tsv>

pixi run -e curation round-init \
    --round-dir .cache/curation/rounds/validation \
    --index .cache/curation/efo-v3.94.0.index.json \
    --chooser stub --fixture <fixture.json> \
    --queue-tsv <validation.tsv>

pixi run -e curation round-gap-scan --round-dir .cache/curation/rounds/validation
pixi run -e curation round-candidates --round-dir .cache/curation/rounds/validation
pixi run -e curation round-choose --round-dir .cache/curation/rounds/validation
pixi run -e curation round-reduce --round-dir .cache/curation/rounds/validation --allow-incomplete
```

### How a curator returns a reviewed queue

A curator edits `review-queue.tsv` in the round directory, filling one row's
`review_decision` (`accept`, `amend`, or `reject`), and for an amend
`override_ontology_id`/`override_ontology_label`, plus `curator_notes`,
`curator`, and `curated_at`. The reviewed file is passed back on a later
promotion run, which promotes accepted/amended rows as `human_reviewed`,
suppresses rejected ones, and preserves every decision in the rewritten queue:

```sh
pixi run -e curation round-promote \
    --round-dir .cache/curation/rounds/ukb-b-2026q1 \
    --reviewed-queue .cache/curation/rounds/ukb-b-2026q1/review-queue.tsv

pixi run -e curation round-coverage \
    --round-dir .cache/curation/rounds/ukb-b-2026q1 --format markdown
```

`curation-round` remains a thin convenience runner: it executes the stages in
order and stops after `reduce` when the round is incomplete, so a failed batch
can be resumed with `round-choose` and then `curation-round` again.

## Running the suites

```sh
pixi run -e curation python tests/curation/test_gap_scan.py
pixi run -e curation python tests/curation/test_candidates.py
pixi run -e curation python tests/curation/test_embedding.py
pixi run -e curation python tests/curation/test_harvest.py
pixi run -e curation python tests/curation/test_recall.py
pixi run -e curation python tests/curation/test_choice.py
pixi run -e curation python tests/curation/test_promotion.py
pixi run -e curation python tests/curation/test_jev_chooser.py
pixi run -e curation python tests/curation/test_validate_chooser.py
pixi run -e curation python tests/curation/test_e2e_pipeline.py
pixi run -e curation python tests/curation/test_coverage.py
# or through the repo orchestrator
pixi run test-python
```

To build the lexicon and the semantic stores, then generate candidates. The
semantic channel needs a HuggingFace text-embeddings-inference server exposing
`POST <base>/v1/embeddings`; set `OPENGWASDB_EMBEDDING_ENDPOINT` to the full
URL and (optionally) `OPENGWASDB_EMBEDDING_API_KEY`:

```sh
# 1. rebuild the pinned lexical index (v3.94.0)
pixi run -e curation ontology-index \
    --obo <efo.obo> \
    --output .cache/curation/efo-v3.94.0.index.json

# 2. embed every ontology term; resumable under <dir>/chunks/
pixi run -e curation embed-ontology \
    --ontology-index .cache/curation/efo-v3.94.0.index.json \
    --output .cache/curation/efo-v3.94.0--BioLORD-2023.ontology-embeddings \
    --model FremyCompany/BioLORD-2023 \
    --endpoint "$OPENGWASDB_EMBEDDING_ENDPOINT"

# 3. precompute the work queue's trait vectors with the SAME model
pixi run -e curation embed-traits \
    --work-queue .cache/curation/queue.tsv \
    --output .cache/curation/efo-v3.94.0--BioLORD-2023.trait-embeddings \
    --model-of .cache/curation/efo-v3.94.0--BioLORD-2023.ontology-embeddings \
    --endpoint "$OPENGWASDB_EMBEDDING_ENDPOINT"

# 4. generate shortlists with no network: ontology + precomputed trait vectors
pixi run -e curation semantic-candidates \
    --work-queue .cache/curation/queue.tsv \
    --index .cache/curation/efo-v3.94.0.index.json \
    --ontology-embeddings .cache/curation/efo-v3.94.0--BioLORD-2023.ontology-embeddings \
    --trait-embeddings .cache/curation/efo-v3.94.0--BioLORD-2023.trait-embeddings \
    --output .cache/curation/shortlist.tsv
```

Each store is a directory (`vectors.npy`, `ids.tsv`, `meta.yaml`);
`embed-ontology` and `embed-traits` write each finished chunk before assembling,
so an interrupted run resumes and only re-requests missing chunks. A label the
trait store does not cover is lexical-only and counted; it is embedded on the
fly only when an endpoint is configured.

To measure the semantic channel's delta over the lexical-only baseline (the
recall report re-run required by issue #166), the offline `local-hashing-v1`
stub keeps the run hermetic (it is plumbing, not a semantic model):

```sh
# build the ontology store with the offline stub
pixi run -e curation embed-ontology \
    --ontology-index .cache/curation/efo-v3.94.0.index.json \
    --output .cache/curation/efo-v3.94.0--local-hashing-v1.ontology-embeddings \
    --model local-hashing-v1

# score the same validation set lexical-only and with the channel enabled
pixi run -e curation recall \
    --validation <validation.tsv> \
    --index .cache/curation/efo-v3.94.0.index.json \
    --enable-embedding \
    --ontology-embeddings .cache/curation/efo-v3.94.0--local-hashing-v1.ontology-embeddings
```

A hosted model such as the pinned `FremyCompany/BioLORD-2023` needs
`--embedding-endpoint` (or `OPENGWASDB_EMBEDDING_ENDPOINT`). An unavailable
channel prints a warning and the report stays lexical-only.

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
    --index .cache/curation/efo-v3.94.0.index.json \
    --chooser stub --fixture <fixture.json> \
    --dry-run

# live round: the confident proposals are promoted and the resource version bumped
pixi run -e curation curation-round \
    --index .cache/curation/efo-v3.94.0.index.json \
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
