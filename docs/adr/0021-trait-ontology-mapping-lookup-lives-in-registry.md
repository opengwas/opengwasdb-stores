# Trait Ontology Mapping lookup lives in the registry, not OpenGWASDB

ADR-0017 puts reusable resolution logic — ancestry assignment, effect-scale validation — in OpenGWASDB, because that logic only needs an Analysis's own data at build time. Trait Ontology Mapping is different: resolving one depends on a Canonical Trait Mapping Table that is curated out-of-band, on its own release cadence, through candidate generation and review rather than pure computation. Coupling table updates to an OpenGWASDB release would block curation fixes on an unrelated dependency's release cycle.

We therefore implement the lookup — trait or gene label against the Canonical Trait Mapping Table, itself a Reference Resource per ADR-0011 — in this repo's `resources/generators/lib/`, called by each Manifest Generator, rather than in OpenGWASDB. OpenGWASDB continues to own the schema shape `trait_ontology_id`/`trait_ontology_label` must satisfy; it doesn't own how a missing value gets filled in. This is a deliberate departure from the ancestry-assignment precedent, not an oversight: read it alongside ADR-0017 rather than as a correction to it.

**Amended by issue #130.** Gene-centric Trait identity does not use the
phenotype-oriented Canonical Trait Mapping Table. When a generator resolves a
gene deterministically against an external authority, it records the gene
symbol in `analysis_label`, the authority-qualified identifier (for example
`ENSEMBL:ENSG00000152256`) in `trait_ontology_id`, the authority name in
`trait_ontology_label`, and `external_authority_lookup` as the mapping method.
This implements upstream OpenGWASDB ADR 0035 without mislabelling a registry
lookup as source-provided or as a Canonical Trait Mapping Table match.
