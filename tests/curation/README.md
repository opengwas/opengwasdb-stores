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
  and the proposals table (issue #167).

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

## Running the suites

```sh
pixi run python tests/curation/test_gap_scan.py
pixi run python tests/curation/test_candidates.py
pixi run python tests/curation/test_embedding.py
pixi run python tests/curation/test_harvest.py
pixi run python tests/curation/test_recall.py
pixi run python tests/curation/test_choice.py
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
