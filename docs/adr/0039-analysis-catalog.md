# The Analysis Catalog is a generated, versioned database over Release Bundles

The API, the website and the R and Python clients (opengwas/opengwasdb#93, #94)
need to find every Analysis across every Store Release, and nothing lists them:
each built Store carries its own `analyses.tsv`, frozen at build time, and the
registry carries Release Bundles. Which Store Releases to serve, which
identifier names an Analysis across Stores, and what the collection holds had
no recorded answer. The owner expects multiple millions of Analyses; decided in
the design of epic #313 on 21 Oct 2026.

The **Analysis Catalog** answers them: the service-catalogue layer ADR 0008
reserved and left to a later decision, a generated, versioned SQLite database
that one operator command rebuilds from the Release Bundles on `dev`.

## Decision

**Package.** The catalog is a top-level package, `src/analysis_catalog/`,
beside `ogstores`, `manifest_generation` and `curation`, and it reads Release
Bundles through the Release Bundle contract in `ogstores`. Nothing in `ogstores`
imports it, so the catalog can move to the API's repository when the API takes
it over. It reads only Release Bundles and never opens a Store, like the master
list (ADR 0023). It never decides which Analyses a Store holds: a Store's
membership is its Release Manifest's answer, and the catalog's filter reuses it
(ADR 0025).

The serving policy lives here and not in the registry, because ADR 0008 puts
default served releases in ADR 0008's service-catalogue layer. The registry keeps
recording facts about releases; the catalog decides what those facts mean for
serving.

**Inputs.** **Membership** is the Release Manifest's rows that are not excluded
from build — the same filter as ADR 0025's derived build manifest. An excluded
row stays in the bundle as an audit row and never enters the catalog.
**Store-level facts** come from `release.yaml`, `build.yaml` and the Validation
Record. **Analysis metadata** comes from the Release Manifest, so the bundle,
including any Release Erratum, is authoritative over a Store's frozen
`analyses.tsv`. A built Store's copy goes stale: OGS-00001 still carries the
`ENSEMBL:` terms the #141 erratum removed, and OGS-00006's Store lacks the
`original_sd` its bundle has for all 1,400 rows.

**Registry fields it needs** are added as release facts, not serving decisions,
so no `served` flag appears. `release.yaml` gains `trial` and `superseded_by`
(#321) and `association_coverage` (#341,
[ADR 0040](0040-association-coverage-is-declared-in-the-release-bundle.md)); the
Validation Record gains `reference_assembly` (#322); and its `n_associations`
comes from a count
OpenGWASDB reports or is null — `register`'s Dense fallback of `n_variants ×
n_analyses` counts matrix cells, not associations, and is removed.

**Identifier.** An Analysis is named `<store_id>:<analysis_id>`, for example
`OGS-00011:GCST000998`.

- It is unique by construction: OpenGWASDB refuses a duplicate `analysis_id`
  within a Store, and `analysis_id` never contains `:` (#319), so it always
  splits into exactly two parts, the Store and the Analysis within it.
- An Analysis never moves between Store ids. When its Store is superseded or
  withdrawn, the catalog keeps its identifier resolvable, so a saved identifier
  names the same row forever.
- A retired Store's Analyses stay as placeholder rows: resolvable, not listed.
  A superseded Store's rows name their successor in `superseded_by`; a
  withdrawn Store has no successor, so its rows carry none. A client holding
  one of their identifiers gets the explanation instead of "not found".

**Serving policy.** Every registered Store Release is recorded with a decision
and its reason, so "why is this Store not in the catalog?" is answerable from
the catalog itself. The rules apply in this order, and the first that matches
decides:

1. bundle.check() returns errors — refused
2. Trial Store Release — excluded
3. Access Posture is embargoed — excluded
4. Release Status is candidate, accepted or built — excluded
5. Release Status is superseded or withdrawn, and the Validation Record is not passed or passed_with_warnings — excluded
6. Release Status is validated, and Access Posture is neither public nor controlled — refused
7. Release Status is validated, superseded or withdrawn, and observed.n_analyses is missing or not an integer — refused
8. the admitted Release Manifest count differs from observed.n_analyses — refused
9. a manifest or Validation Record value cannot be stored exactly, or check() tolerates ':' in an analysis_id under #319 — refused
10. Release Status is superseded or withdrawn — placeholder
11. Release Status is validated — listed

Rule 5 excludes, and does not refuse, a retired release with no passing
Validation Record: it was never served, so there is no fault to report. A retired
release that did pass, but records no count, is refused by rule 7 as a `validated`
one is.

The order matters because the first two rules are not about Release Status: a
Trial Store Release or an `embargoed` Store is never resolvable, not even after
it is retired, so none of its Analyses can leak out through a placeholder row.
A listed `controlled` Store carries its Access Posture so that the service
enforces access itself; the catalog states the posture and does not enforce it
(ADR 0008). A refused Store fails loudly: the catalog never lists a membership
the Store does not hold, and a missing count is an absence, never a match.

**Schema and build.** Two main tables and supporting ones, in one SQLite file:
`stores`, one row per listed or placeholder Store Release (an excluded or
refused Store has none, so nothing excluded is discoverable there); `analyses`,
one row per Analysis of a listed or placeholder Store, with `listed` telling
them apart; `selection`, the decision and reason per registered
Store Release; `catalog_meta`, the build's own facts; and derived tables and
indexes for counts, search and paging.

- Store-level facts are stored once, never repeated per Analysis, and no Store
  path is stored: it is a pure function of the id (ADR 0030).
- Build internals are left out — `source_file`, checksums, `inclusion_reason`,
  `exclude_from_build`, `analysis_index` — because they locate and justify a
  build input, which is not what a reader queries.
- Numeric columns are typed, and `analysis_label` falls back to `source_label`,
  because the MVP bundles have no `analysis_label`. `label_from_source` records
  each fallback, so the summary report can count Analyses with no curated label.
- **The type of an Analysis is a set of columns of `analyses`**, derived once at
  build time from fields the row already holds: `outcome_type` (binary or
  continuous) from `stored_effect_scale` and `sample_size_kind`, binary when
  either says so (`case_control` or `log_or`), `NULL` when neither field gives
  a reading; `trait_class` (phenotype or molecular) from `context`, `tissue`
  and `trait_chr`; and `ancestry_category`, Assigned Ancestry or `META` for an
  unassigned row whose Source Ancestry Label is exactly `META`. Genome-wide
  or cis is not derived: it is the Store Release's declared Association
  Coverage (`release.yaml`, one of `gwas`, `cis`, `cis_and_signals` and
  `signals_only`), counted as it is and never relabelled, and never a layout's
  default. The counts and the report read those columns, so a type is derived
  once and defined in one place.
- **A placeholder row is an `analyses` row with `listed = 0`.** Listing,
  search and counts see only `listed = 1`, through the `listed_analyses` view,
  and the paging indexes are partial indexes over those rows, so a retired
  Store costs a listing nothing.

Every rebuild is a full rebuild into a fresh database, one Store's rows at a
time (so memory is set by the largest single Store's manifest, not by the
catalog), done in SQL, so a
version is never a mixture of two inputs and a failed build leaves the previous
version in place. The prototype's raw SQL fold, measured on a replication of the real catalog
at 5.07M rows, took 39 s for a 2.3 GB file, with lookups, full-text search and
first pages under 1 ms, a 4M-row `OFFSET` at 65 ms and a filtered count at
59 ms. This builder validates and converts every row in Python, and has been
measured only at 500k rows in one wide Store: about 25 s and 0.8 GB peak, with a
no-op rebuild that reads no manifest row (a few seconds at that size, all of it
hashing the files). It is not extrapolated beyond that. So the indexes
support server-side paging, including keyset paging; counts per filter value
are computed at build time, because a filtered count is the expensive query and
only changes when the catalog does; and the full-text index is FTS5, over the
label, ontology label, consortium and first author.

**Versions and location.** A version is a directory under
`<artifact-root>/catalog/versions/`, and `current` is a symlink to it:

```
versions/<version>/
  catalog.sqlite
  stores.tsv
  analyses.tsv.gz
  summary.html
  report.md
current -> versions/<version>
```

- **The version is a hash of everything a decision depends on**: every file of
  every registered Release Bundle, the list of registered store ids, the source
  of `analysis_catalog` (which includes the serving policy) and of `ogstores`
  (`check()` and `Bundle`), the installed opengwasdb's version and commit (its
  reader parses the manifest), and the summary template. Decisions are a pure
  function of those inputs, so they are not hashed, and the version is known
  before any manifest row is read. The bundle inputs are wider than the
  selected bundles' contents on purpose:
  `bundle.check()` reads every file of every bundle, so an edit to an excluded
  Store's manifest can change its decision, and a change to a decision, from a
  bundle or from the code that reads it, must give a new version, or the
  reasons in `selection` go stale. A rebuild whose version
  already exists writes nothing and reads no manifest row: it reports the
  decisions recorded in that version's `selection`.
- **A version is immutable and the last few are kept**, so rollback is moving
  `current` back: a rebuild writes a new directory and swaps `current`
  atomically, no reader sees a half-written catalog, and nothing is
  reconstructed from git. The files and directories are fsynced before the
  rename and the swap, because an immutable version that a crash tore would
  never be rebuilt.
- The catalog is a generated artifact under the Artifact Root with no Store id,
  because it belongs to no single Store Release. Bulk download is TSV.gz beside
  the SQLite file, so a client that wants every Analysis reads a flat file, and
  the API and website read `current` (#173).

**Trigger.** An operator runs the command on the host after a merge to `dev`,
from a dedicated clean checkout of `origin/dev`, not a shared working checkout.
The catalog depends only on bundles, so a rebuild then picks up everything
merged: a Store Release Build publication alone changes nothing, and a Store
enters the catalog when its reviewed `validated` edit merges. Automating the
trigger is out of scope.

**Summary report.** A Quarto report is rendered into each version before
`current` is swapped, and a failed render fails the rebuild, so a published
version always has its report. Every number in it comes from the catalog's own
tables, so it cannot disagree with what the service serves. It covers the
served Stores, the excluded and refused ones with their reasons, Analyses by
Assigned Ancestry and by type, Trait Ontology Mapping coverage, the change
since the previous version, and every total, where a total is shown as "unknown
(k of n Stores report it)" whenever a Store reports none, so a partial sum is
never presented as the total.

## Rejected alternatives

**A bare global `analysis_id`, kept unique by a registry-wide check.** Rejected.
`analysis_id` is not a global key. Today's overlaps are lineage and
trial-then-full pairs, but gene-and-tissue identifiers (`ENSG…::whole_blood`)
collide between eQTL sources, and the MVP dbGaP release re-serves GWAS Catalog
`GCST` accessions. A registry-wide check would answer that by refusing the
second Store: a legitimate Store's membership would then depend on another
Store's, which is a decision about what a Store holds — the one thing the
registry never makes.

**Serving whatever is on disk rather than gating on Release Status.** Rejected.
Most of what is on disk is not ready: the prototype found OGS-00008–11 and
OGS-00017 built but `candidate` or `accepted` with no `publication.json`, so a
disk-driven selection listed 9,347 Analyses where a status-driven one listed
1,400. It would also serve Trial Store Releases and `embargoed` Stores, which
nothing but metadata marks, and it fails the wrong way: an unreviewed
publication becomes visible to users and nothing can say why. `validated` makes
the reviewed edit that validates a release the decision to serve it. Disk
membership is stale besides: OGS-00011's published Store holds 148 Analyses its
current bundle excludes.

**Parquet as the served format.** Rejected. The served file has to answer
full-text search, server-side paging with a cursor, and per-filter counts, and
the prototype's one SQLite file met all three at 5.07M rows within a 39 s build
and 2.3 GB. A
Parquet dataset has no full-text index and no per-row addressing, so each of
those queries would need a second, derived structure beside it, maintained in
step with the first. It gains readers nothing either: bulk download is a plain
TSV.gz, which any client can read without a columnar engine. Parquet remains a
possible export alongside it, not the served store.

## Consequences

- **Association Coverage is the release's declaration, not the builder's
  report** (#341). The catalog reads `release.yaml` for
  `stores.association_coverage`, the `coverage` count dimension and the summary
  report; the Validation Record's `observed.association_coverage` stays what
  opengwasdb reported, and is not what the catalog serves. This ADR's earlier
  statement that the Validation Record gains the field is superseded by ADR
  0040.
- **CONTEXT.md gains Analysis Catalog**, and the Store Registry entry's
  `_Avoid_: catalogue` now forbids the word for the registry rather than for the
  catalog.
- **ADR 0008's service-catalogue layer is now built** and named. Its boundary
  is unchanged: routing, authorisation enforcement, billing, quotas and usage
  audit still belong to the service.
- **The catalog is empty today.** The only `validated` Store Releases are
  OGS-00002 and OGS-00005, both Trial Store Releases, so the selection lists
  nothing until the API-ready Stores reach `validated`.
- **Per-Analysis facts that only a built Store holds are not carried**, such as
  hit counts; a later version can take them from a sidecar of OpenGWASDB's
  output.
- **The API, the website, the client packages, the trigger automation and a
  Parquet export are out of scope** here. The code follows in the catalog's
  command and versions (#323), its search, counts, paging indexes and
  placeholder rows (#324) and its summary report (#325). This adds no code.
