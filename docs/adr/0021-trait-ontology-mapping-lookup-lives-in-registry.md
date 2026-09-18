# Trait Ontology Mapping lookup lives in the registry, not OpenGWASDB

ADR-0017 puts reusable resolution logic — ancestry assignment, effect-scale validation — in OpenGWASDB, because that logic only needs an Analysis's own data at build time. Trait Ontology Mapping is different: resolving one depends on a Canonical Trait Mapping Table that is curated out-of-band, on its own release cadence, through candidate generation and review rather than pure computation. Coupling table updates to an OpenGWASDB release would block curation fixes on an unrelated dependency's release cycle.

We therefore implement the lookup — trait label against the Canonical Trait Mapping Table, itself a Reference Resource per ADR-0011 — in this repo's `resources/generators/lib/`, called by each Manifest Generator, rather than in OpenGWASDB. OpenGWASDB continues to own the schema shape `trait_ontology_id`/`trait_ontology_label` must satisfy; it doesn't own how a missing value gets filled in. This is a deliberate departure from the ancestry-assignment precedent, not an oversight: read it alongside ADR-0017 rather than as a correction to it.

**Amended by issue #130.** Gene-centric Trait identity does not use the
phenotype-oriented Canonical Trait Mapping Table. When a generator resolves a
gene deterministically against an external authority, it records the gene
symbol in `analysis_label`, the authority-qualified identifier (for example
`ENSEMBL:ENSG00000152256`) in `trait_ontology_id`, the authority name in
`trait_ontology_label`, and `external_authority_lookup` as the mapping method.
This implements upstream OpenGWASDB ADR 0035 without mislabelling a registry
lookup as source-provided or as a Canonical Trait Mapping Table match.

**The #130 amendment is corrected by issue #141.** The amendment put a gene
identifier where a Trait Ontology term belongs and an authority name where a
trait label belongs, silently asserting that a gene *is* the Trait; on a SomaScan
aggregate assay it promoted one arbitrary member of a multi-protein family to
the Analysis identity. Issue #141 reverses it: `trait_ontology_id` and
`trait_ontology_label` carry only the source-provided (or Canonical Trait Mapping
Table) ontology term and its trait label, or are left empty when no acceptable
term exists. Gene, transcript, and UniProt identity is Target annotation -- in
the target sidecar, `resources/reference-resources/somascan/somascan-targets.tsv`,
and `trait_chr`/`trait_bp` -- never a Trait Ontology Mapping, and an aggregate
assay is identified by its SomaScan SeqId rather than one member gene.
`bundle.check()` rejects a gene-shaped `trait_ontology_id` and an authority-name
`trait_ontology_label` so the class cannot recur (issue #141).
