"""Curation tooling for OpenGWASDB Store Releases.

This package holds repository-owned curation stages that operate *over*
committed Release Manifests without changing them:

- :mod:`curation.gap_scan` derives the unmapped Trait work queue that feeds
  Canonical Trait Mapping Table curation (issue #163).
- :mod:`curation.ontology` pins the ontology release and builds the rebuildable
  retrieval index candidate generation resolves against (issue #164).
- :mod:`curation.candidates` turns each queued Trait label into a
  multi-channel lexical shortlist of plausible ontology terms (issue #164).
- :mod:`curation.embedding` builds and queries the semantic embedding index
  that the optional `embedding` channel adds to candidate generation, and
  carries the pinned model and content-addressed index build (issue #166).
- :mod:`curation.harvest` collects the source-provided Trait Ontology Mapping
  pairs as a ground-truth validation set (issue #165).
- :mod:`curation.recall` scores retrieval against that validation set and
  reports stratified recall with the ukb-b stratum-gap caveat, including the
  semantic channel's delta over the lexical-only baseline (issues #165/#166).
- :mod:`curation.chooser` defines the chooser interface and its shortlist
  membership rule (issue #167).
- :mod:`curation.stub_chooser` replays recorded choices for hermetic testing of
  the choice stage (issue #167).
- :mod:`curation.choice` runs a chooser over each shortlist and emits the
  proposals table (issue #167).
- :mod:`curation.promotion` gates proposals on confidence and runner-up
  margin, promotes the confident ones to the Canonical Trait Mapping Table
  with a Reference Resource version bump, and queues the rest for review
  (issue #169).
"""
