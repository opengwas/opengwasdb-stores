# Analysis Catalogue as the ingestion hub; stores are subset views

Status: superseded by the `opengwasdb-stores` registry (Source Collection → Store
Family → Manifest Generator → Release Manifest), and by ADR 0030. That registry
does the same job — annotate-then-subset selection of which Analyses go in which
store — in a separate repository, better specified, and versioned. This ADR's
premise that `opengwasdb` itself must persist the Catalogue TSV as "the source of
truth for per-Analysis facts" no longer holds once a Store Release carries its own
Analytical Metadata (ADR 0030): a downloaded store no longer needs a catalogue
service, of either repo's design, to be interpretable. `opengwasdb`'s role narrows
to reusable annotator functions (ancestry assignment, effect-scale standardisation)
callable by `opengwasdb-stores`' Manifest Generators, with no TSV of its own.

OpenGWAS is large and heterogeneous (tens of thousands of Analyses across many
consortia, ancestries, and shapes). The long-term intent is to split it into
several **right-shaped, manageable stores** — dense where a shared panel fits,
ragged where cis-only and sparse, hybrid where genome-wide with a common core —
rather than one monolith. Deciding *which Analyses go in which store* needs
per-Analysis facts that only emerge from looking at each Analysis: its recovered
ancestry, whether it carries allele frequencies, whether it lifts over cleanly,
its sample size and consortium. Historically the builder was handed a bespoke
`manifest.tsv` (`trait_id`, `file_path`, `trait_name`, `n`) per store, so those
facts were re-derived ad hoc per build and never recorded in one place.

## Decision

Introduce an **Analysis Catalogue**: the complete list of candidate Analyses for a
collection, each row annotated once — during ingestion — with everything needed to
decide store membership (**Assigned Ancestry** and **Ancestry Composition**,
allele-frequency availability, liftover feasibility, sample size, source
metadata, **Reported Population**). Annotation is a pipeline of independent
*annotators* that write into the Catalogue; ancestry assignment (ADR 0028) is the
first.

**A build consumes a subset view of the Catalogue, not a bespoke manifest.** A
build manifest is a row-filter over the Catalogue (e.g. `Assigned Ancestry ==
EUR`). Because the builder already reads only its four columns via `DictReader`
and ignores the rest, the Catalogue is a **superset of the build manifest** in the
same TSV format: subsetting is `filter()`, with no format translation.

The Catalogue is **versioned** — Analyses are added over time, and annotations
(e.g. Ancestry Composition) are recomputed when their inputs change (e.g. a new
Ancestry Reference Panel). Each store records the `catalogue_version` and the
subset filter that produced it, as provenance.

Scope of the first Catalogue: the `ieu-a` / `ieu-b` consortium collections. The
schema is designed to graduate to all of OpenGWAS and to a SQLite backing store,
but neither is built now.

## Considered options

- **Bespoke per-store manifests (status quo).** Rejected: re-derives per-Analysis
  facts ad hoc at every build, records them nowhere, and gives no single place to
  reason about how to partition a collection into stores.
- **A SQLite catalogue database from the start.** Rejected *for now*: richer
  querying, but unjustified complexity for a few hundred rows. The flat TSV drops
  straight into the existing manifest flow and is diffable in git; the schema is
  designed so the same columns move into SQLite when the Catalogue spans tens of
  thousands of Analyses.
- **Ancestry as a bespoke build-time router** (label Analyses inside the builder).
  Rejected: couples ancestry inference to the build, hides the decision, and can't
  be reused by the other partitioning axes (frequency-of-AF, sample size, shape).
  Annotation belongs upstream of, and independent from, any single build.

## Consequences

- Ingestion becomes a modular annotate-then-subset pipeline; annotators (ancestry,
  AF-availability, liftover feasibility, …) are independent and composable.
- The Catalogue, not the store, is the source of truth for per-Analysis facts. A
  store inherits them via the subset it was built from and records the Catalogue
  version + filter as provenance.
- The same Catalogue supports many partitioning axes (ancestry now; shape,
  consortium, size later), so "split OpenGWAS into right-shaped stores" is a matter
  of choosing subset filters, not writing new routers.
- The Catalogue must be versioned and its annotations reproducible from recorded
  inputs, so a store's membership can always be explained after the fact.
