# Curation test suites

Hermetic, network-free test suites for the Canonical Trait Mapping Table
curation pipeline (issue #161):

- `test_gap_scan.py` — the unmapped Trait work queue (issue #163);
- `test_candidates.py` — pinned ontology release and lexical candidate
  generation (issue #164);
- `test_harvest.py` — the source-provided validation set (issue #165);
- `test_recall.py` — stratified retrieval recall and the ukb-b stratum gap
  (issue #165);
- `test_choice.py` — the chooser interface, the fixture-backed stub chooser,
  and the proposals table (issue #167);
- `test_promotion.py` — the confidence/margin gate, the review queue, rejection
  persistence, and the Reference Resource version bump (issue #169).

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
6. **CLI surface**: the command scores against an index or pre-generated
   shortlists and writes the report to stdout or `--output`.

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

## Running the suites

```sh
pixi run python tests/curation/test_gap_scan.py
pixi run python tests/curation/test_candidates.py
pixi run python tests/curation/test_harvest.py
pixi run python tests/curation/test_recall.py
pixi run python tests/curation/test_choice.py
pixi run python tests/curation/test_promotion.py
# or through the repo orchestrator
pixi run test-python
```
