# Curation test suites

Hermetic, network-free test suites for the Canonical Trait Mapping Table
curation pipeline (issue #161):

- `test_gap_scan.py` — the unmapped Trait work queue (issue #163);
- `test_candidates.py` — pinned ontology release and lexical candidate
  generation (issue #164);
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
pixi run python tests/curation/test_choice.py
# or through the repo orchestrator
pixi run test-python
```
