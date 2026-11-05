# Store Audit

The specification of the read-only Store Audit commands (stores #372 and #373,
epic #371). It follows
[ADR 0023](../adr/0023-the-registry-store-seam-is-a-command-line.md) (the seam
is a command line) and leaves source reading, Store access and format semantics
with `opengwasdb` (ADRs 0012 and 0017). The words are
[CONTEXT.md](../../CONTEXT.md)'s. This file is the contract: the commands, what
they read, what their reports say, and how a reader is meant to act on them.

Two commands share one module and one read-only discipline:

- `reconcile` (#372) reconciles the Store's **axis** -- the shared variant axis,
  its Dense Component and the off-panel remainder -- against what the Store
  records.
- `attribute` (#373) attributes the off-panel union to the **Analyses and
  publications** that observe it.

`reconcile` never reads the Ragged body's variant indices, and `attribute` never
reconciles a count it did not measure. A reader who wants "how big is the
off-panel part" reads `reconcile`; "whose is it" reads `attribute`.

A third command, `trace` (#374), followed a seeded sample of rows back to their
raw source rows and replayed the builder's admission rule on each. It was
removed, with the sample `attribute --sample-output` wrote for it, by the
owner's decision of 5 Nov 2026 (#428). Epic #371 had closed on 1 Nov, and the
rule it replayed, `opengwasdb.build.row_admission.admit_rows`, is gone from
opengwasdb 0.5.0. That release's builders write an Association Admission record
beside every Store they build, which counts every dropped row and logs it by
source key, so "why was it admitted" is now read from the build's own record
(`store.opengwasdb.admission`, [the workflow spec](store-release-workflow.md)).
The trace's measurements on OGS-00011 are kept below as history.

## The command

```text
python3 -m ogstores.audit reconcile <store_path> --output <evidence_json>
pixi run audit-store reconcile /data/opengwasdb/stores/OGS-00011/store.opengwasdb \
  --output /data/opengwasdb/work/epic371/orchestrate/opengwasdb-stores-371/evidence/372/reconciliation.json
```

| Option | Meaning |
|---|---|
| `--reference PATH` | reconcile the off-panel partition against this construction variant reference instead of the one the Store records |
| `--skip-reference` | do not reconcile against a reference at all. Recorded as `skipped`, removed from `scope.measured`, added to `scope.not_measured`, and the run reports `ok: false` (exit 1). Refused together with `--reference` |
| `--no-identities` | skip the variant-table pass (duplicate ALIDs, multiallelics, reference membership). Recorded as `scanned: false`, with the same `ok: false` consequence |
| `--band-variants N` | axis entries one band of the occupancy scan spans (default 4,000,000). The answer does not depend on it |
| `--verify auto\|sha256\|metadata` | how the Store is proved unchanged: `sha256` every file, `size+mtime_ns` only, or `auto` (sha256 up to 512 MiB) |

```text
python3 -m ogstores.audit attribute <store_path> --output <evidence_json>
pixi run audit-store attribute /data/opengwasdb/stores/OGS-00011/store.opengwasdb \
  --output /data/opengwasdb/work/epic371/orchestrate/opengwasdb-stores-371/evidence/373/attribution.json
```

| Option | Meaning |
|---|---|
| `--top N` | how deep the union-share measurement goes (default 5) |
| `--band-variants N` | axis entries one band of the attribution scan spans (default 8,000,000). The answer does not depend on it |
| `--band-rows N` | association rows one band of the support scan spans (default 16,000,000, 64 MB of `int32`). The answer does not depend on it |
| `--verify auto\|sha256\|metadata` | as `reconcile` |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | reconciled, and `ok` is true: every disagreement is a `finding` |
| 1 | reconciled, and `ok` is false: an `error` (the Store's artifact and its own record disagree, or evidence the audit requires is missing or contradicts itself), or a stage the caller explicitly skipped |
| 2 | the Store cannot be audited at all: no manifest, not a hybrid Store, a required artifact absent, a bad invocation |

A `2` is never a report about the Store, and a `1` is never a pass. `ok` is the
exit code: it is true exactly when no `error`-severity inconsistency was
recorded, and skipping a stage is one of those -- a run that did not measure a
required partition must not be read as an exact, closed reconciliation. The
command exits `2` rather than writing a report that reconciles something other
than the Store the caller named, and refuses an `--output` inside the Store.

## Scope

One slice, and deliberately not more: `reconcile` reconciles the **axis**, not
its causes. It reads the Store Release's own artifact and its own record --
`manifest.json`, `variants.tsv.gz` and its ALID search index, `data.zarr`'s
`ragged/offsets`, `ragged/variant_index` lengths and `ragged/by_variant/offsets`,
`dense/dense_to_shared.npy`, `analyses.tsv`, and `records/build.json` beside it
-- and the construction reference the record names. It never reads a Build
Recipe, so a Store whose recipe has moved on is still audited as built.

What it does not measure is in the report's `scope.not_measured`, with the
ticket that owns each: per-Analysis and per-publication attribution and support
across Analyses (both measured by `attribute`, below), source frequency evidence
(#374, which `trace` measured until it was removed), and rsid groups (#375).
Their absence is stated, not left to be inferred from a report that has no field
for them.

`attribute` is the other half of the same slice: it names the contributors and
stops at the Store's edge. It does not read a source file, so it cannot say
*whether* an association should have been admitted -- only that a given Analysis
contributed it. That question is now the build's Admission record's.

## Prerequisites, and what makes them an `error`

The Store's own evidence is required, not optional. Each of these is an
`error`-severity inconsistency, so the run reports `ok: false` and exits 1
rather than passing with a blank where a fact should be:

| Missing or contradicted | Kind |
|---|---|
| `records/build.json` absent | `build_record_unavailable` |
| `records/build.json` unreadable, or not a JSON object | `build_record_unreadable` |
| `records/build.json` with no `opengwasdb_rev` | `build_revision_unrecorded` |
| `analyses.tsv` absent | `analyses_tsv_unavailable` |
| `analyses.tsv` undecodable | `analyses_tsv_unavailable` |
| provenance missing any of `n_variants`, `n_analyses`, `n_panel`, `n_off_panel`, `n_overflow_associations`, `ragged.by_variant.n_axis`, `ragged.by_variant.n_rows` | `recorded_counts_incomplete` |
| any of those counts disagreeing with the artifact | `recorded_*_disagrees` |
| an ALID search index whose two arrays differ in length | `alid_index_half_written` |
| an ALID search index that is not sorted | `alid_index_not_sorted` |
| an ALID search index whose entry count differs from the table's indexable rows | `alid_index_disagrees_with_table` |
| a sampled row whose ALID contradicts its own chromosome, position or allele columns | `alid_sample_mismatch` |
| a Dense-axis variant carrying Overflow associations (disjoint partition) | `dense_axis_rows_carry_overflow_associations` |
| a Store directory replaced or removed during the audit | `store_replaced_during_audit`, `store_vanished_during_audit` |
| a release lock taken while the audit was reading the Store | `release_started_during_audit` |
| any file in the Store whose size, mtime or hash changed | `store_modified` |
| a stage the caller skipped | `skipped_evidence` |

The ALID cross-check is a *Store-level* check and does not replace
`opengwasdb`'s variant normalisation (ADR 0017): it asks whether a stored row's
`alid` string agrees with that row's own columns, not whether the identity the
builder derived from a source row was the right one -- that is the engine's, and
a change to it is the engine's change (ADR 0023's seam is a command line, and
this command consumes it). Its sample is every 100,000th row, recorded as
`identities.alid_consistency_sample` with the stride, so a reader knows the
check's power rather than inferring a census from it.

## A stable, read-only view

A Reference Completion replaces a Store under its own id (ADR 0037), so the
thing an audit describes has to be one directory and not two. Two independent
guards, both recorded in `store.stable_view`:

- the Store directory's device and inode (and its parent's) before and after --
  a rename swap changes them, and the report then carries
  `store_replaced_during_audit`;
- the release lock (`ogstores.release.lock_holder`, a non-blocking shared
  `flock` on `<artifact-root>/.release-locks/<store_id>.lock`), probed at the
  start and at the end. A lock held throughout is a `finding`
  (`release_lock_held`) because a long-lived orchestrator can hold one without
  touching this Store; a lock *taken* during the audit is an `error`. The probe
  creates and removes nothing, so asking cannot change the release.

## What it reconciles

| Fact | Read from | Reconciled against |
|---|---|---|
| shared-axis length | `VariantAxis` over `variants.tsv.gz` | recorded `provenance.n_variants`, table row count |
| Dense-axis length | `dense/dense_to_shared.npy`, the Dense Component's own axis | recorded `provenance.hybrid.n_panel` |
| off-panel length | axis minus the distinct mapped rows | recorded `provenance.hybrid.n_off_panel` |
| observed occupancy | `ragged/by_variant/offsets` in bands: `counts = diff(offsets[lo:hi+1])` | the Analysis-major `ragged/offsets`, the recorded `n_overflow_associations` |
| variant-major index span | `ByVariantReader` (spans the axis, not stale against the component) | recorded `provenance.ragged.by_variant` |
| Analyses | `ragged/offsets` entries minus one | `analyses.tsv` rows, recorded `n_analyses` |
| canonical identity | every `variants.tsv.gz` row, plus the sorted ALID search index | the axis: one row per canonical ALID |
| construction reference | `read_variant_reference` on the recorded path | the axis, and the Dense Component's row set |

Canonical identity is counted twice on purpose. The table streaming pass counts
duplicates only among rows sharing a position -- the axis is position-ordered --
while the sorted ALID search index counts them exactly, with no ordering
assumption, by counting adjacent equal entries. The rows whose ALID is wider
than the index's fixed 32-byte key are counted from the table instead, because
the index leaves them out ([`opengwasdb` issue #127](https://github.com/explodecomputer/opengwasdb)).
Two counts that agree are the evidence that either is right.

The construction reference is recorded by identity (path, size, mtime, sha256,
ALID count) because a panel that moves cannot be reconciled with afterwards.
`role` states which reference it is: the build's own construction panel, not the
LD panel a Reference Completion uses (ADR 0037).

## What `attribute` attributes

Four questions, each answered exactly, from the Store's own arrays:

| Question | Read from | Reported as |
|---|---|---|
| how many distinct Analyses observe each off-panel entry | each Analysis's row range of `ragged/variant_index`, streamed in bands | `support.analyses`, bucketed 1, 2, 3-10, 11-100, 101+ |
| how many distinct publications observe it | the same rows, coded to publications through `analyses.tsv` | `support.publications` |
| is it observed by exactly one Analysis, or by Analyses of exactly one publication | the same two planes | `support.exclusive_to_one_analysis_variants`, `support.exclusive_to_one_publication_variants`, and `..._multi_analysis_variants` for the publication-only case that several Analyses share |
| whose it is, how much each holds, and how much of the union the leaders explain | the planes per axis band, then exact unions of the leading contributors' own variant lists | `contributors.analyses`, `contributors.publications`, `concentration`; the full tables are `evidence/373/analyses.tsv` and `evidence/373/publications.tsv` |

The support plane is two `uint16` arrays over the axis (distinct Analyses,
distinct publications) plus their two observer planes and two deduplication
stamps -- 1.4 GB on OGS-00011's 114,514,113-entry axis, and the scan never holds
the 2.9B-row body: one band of `ragged/variant_index` at a time.

Two details are load-bearing rather than tidy, and the suite pins each:

- **Analyses are visited in publication order**, not index order, and each
  block is deduplicated within itself before the stamps are consulted. A stamp
  means "already counted for this publication", so a publication's Analyses must
  be contiguous: in index order an Analysis of publication B can stamp an entry
  between two Analyses of publication A, and A's second Analysis then counts it
  twice. And two rows for one variant inside one band are two stamps that both
  read as new, so the block is `np.unique`d first.
- Consequences are reported rather than hidden. `repeated_variant_rows_in_one_analysis`
  and `analysis_variant_lists_not_increasing` are `finding`s, and
  `single_observer_variants_carry_several_associations` is a `finding` too: an
  exclusive *variant* count and an exclusive *association* count are kept apart
  and both reported, so a Store that repeats a row cannot make one stand in for
  the other.

A publication is `analyses.tsv`'s `publication_pmid`, or its `publication_doi`
when no pmid is recorded. An Analysis with neither is retained as its own
explicitly unknown group (`publication_key_kind: "unknown"`, key
`unknown:<analysis_id>`, reported by the `publication_metadata_unknown`
`finding`): unknowns are neither dropped nor coalesced, because one merged
"unknown" would report two unrelated Analyses as one publication's. One
publication recorded with two DOIs or two first authors
(`publication_metadata_ambiguous`) is an `error`: attribution that guessed
between two publications would be a wrong answer that looks like a right one.

All metadata is indexed by `analysis_index`, never by `analyses.tsv` row order,
and the file's own numbering is checked against the axis
(`analysis_index_disagrees_with_position`, an `error`, and a `2` when the
numbering cannot be read against the Ragged partition at all).

`concentration` is the epic's question about attribution rather than a sum. Each
leading contributor's variant list is unioned exactly in one boolean plane over
the axis, and `top_analyses`/`top_publications` report the union at prefixes 1,
2, 5, 10 and the last contributor, plus its share of `census.attributed_union_variants`.
A share is never the sum of the contributors' counts: two Analyses of one
publication share most of their variants, and the report says
`"numerator": "distinct off-panel variants of the prefix's union, not the sum of theirs"`.

`allele_classes` and `chromosomes` come from one stream of the Store Variant
Table, off-panel rows only, classified `snp` (one base against one base),
`indel` (alleles of different lengths) or `complex` (two equal-length alleles
longer than one base). A position's row group is counted separately
(`multiallelic_positions`, `rows_at_multiallelic_positions`) so a site's row
count is never confused with a row's class.

`census` reconciles the attributed union against the two accounts of its size:
the axis minus the Dense Component, and the `n_off_panel` the Store records.
`attributed_union_variants` counts off-panel entries at least one Analysis
observes. On `OGS-00011` all three agree at 101,098,177.

## The report

`schema` is `ogstores.audit.reconcile/1`; `audit_version` is separate and
changes only if the report's meaning does. Adding a field does not bump either.
`audit_version` `2` (stores #380) was the trace's admission fields changing
meaning with opengwasdb#331; the reconcile report itself is unchanged and takes
the version because the commands share one. With `trace` removed (#428) the
version stays `2`, and is not reused.

| Field | Contents |
|---|---|
| `argv`, `started_at`, `finished_at`, `wall_seconds`, `runtime` | what ran, when, and against which `opengwasdb`/Python/NumPy; `audit_version` beside them |
| `store` | path, `manifest_sha256`, id, layout, format version, completion state, `opened_mode: "r"`, `build_revision` (the `opengwasdb` rev from `records/build.json`, with the `problem` that made it unusable when it is), `stable_view` (directory identity and release lock), `recorded` (every claim reconciled above), `analyses_tsv_rows` |
| `reference` | identity of the construction reference, its ALID count, the axis rows in it and absent from it, and whether the Dense Component's row set is exactly that set |
| `axis` | `n_axis`, `n_dense`, `n_off_panel`, the mapping's own checks (`distinct_entries`, `duplicate_entries`, `strictly_increasing`, `out_of_range`), `reconciled` |
| `occupancy` | `axis`, `dense` and `off_panel`: associations, observed and unobserved variants, single-association variants, the largest, and for off-panel the first 20 unobserved axis indices; plus a support histogram and the band scan's own checks |
| `components` | Analyses, association totals, index lengths, and `index_validated_by` -- the engine interface that validated the index, not this module |
| `identities` | rows, distinct ALIDs, duplicate groups and extra rows (with `duplicate_alid_source`), the search index's own lengths, sortedness and agreement with the table, non-canonical rows, multiallelic sites and rows, missing rsids, and the ALID/column cross-check sample |
| `inconsistencies` | one entry per disagreement: `kind`, `severity` (`error` or `finding`), `detail`, and the counts that justify it |
| `ok`, `error_count`, `finding_count` | the exit code's own summary |
| `unchanged` | the read-only proof: `method`, `files`, and `changed`/`added`/`removed` |
| `resources` | band size, band count, peak RSS (which includes the reference's ALID set), so a rerun can be planned |
| `scope` | `measured` (the stages this run actually ran), `not_measured` with the ticket that owns each, and `skipped` naming any stage a flag switched off |

`occupancy.dense` is not a finding waiting to happen: a Dense-axis entry carries
no Ragged Overflow association by the disjoint partition, because its
associations are in the Dense Component. Only the off-panel axis's unobserved
entries raise `unobserved_axis_entries`, and unobserved entries are a `finding`,
not an `error` -- the epic asks how many there are, not whether they are legal.

`attribute`'s report is `schema: "ogstores.audit.attribute/2"`, with the same
`argv`, `started_at`, `finished_at`, `wall_seconds`, `runtime`, `store`,
`inconsistencies`, `ok`, `error_count`, `finding_count`, `unchanged`,
`resources` and `scope` fields as `reconcile`, plus the fields below. `/1`
also carried `sample` and `scope.skipped`, which went with `trace` (#428). Its
`scope` has no `skipped`, because nothing in it can be switched off.

| Field | Contents |
|---|---|
| `axis` | `n_axis`, `n_dense`, `n_off_panel`, and the association rows the Ragged body yielded |
| `census` | the recorded `n_off_panel`, the axis complement, the attributed union, whether they agree, and the association count |
| `support` | the Analyses and publications histograms, the exclusive counts, and the arithmetic check that the per-contributor sums are the same quantities |
| `contributors` | the Analysis and publication rankings by exclusive variants, associations and distinct variants, each with its metadata; the full per-Analysis and per-publication tables are written beside the evidence JSON as `analyses.tsv` and `publications.tsv`, with their `sha256` and row counts recorded in `contributors.tables` |
| `concentration` | the leading contributors' exact union and share at prefixes 1, 2, 5, 10 and the last |
| `allele_classes`, `chromosomes` | the union's shape |
| `resources` | band sizes, the support scan's band count, the variant table rows read, and peak RSS |

## Read-only

Three properties, all checked rather than promised:

1. The release is opened with `opengwasdb.store.open.open_store` and its arrays
   with `arrays(mode="r")`; every array is loaded `mmap_mode="r"`. Nothing is
   written but `--output`, which may not be inside the Store.
2. Every file's size and mtime -- and its sha256 unless the Store exceeds the
   hashing limit -- is recorded before and after the audit and compared. A
   difference is an `error` (`store_modified`) and exits `1`; the report says
   which method it used, so "unchanged" is a claim with a measurement behind it.
3. The evidence file is written through `tempfile.mkstemp` in the output's own
   directory and renamed onto `--output` with `os.replace`. A predictable
   `.tmp` sibling can pre-exist as a file or a symlink for this run to truncate;
   an unguessable one cannot. The staged path is checked against the Store
   before a byte is written, and a symlink already at `--output` is replaced
   rather than followed.

Large artifacts stay outside Git, under the deployment's evidence directory
named in the ticket. Small reviewed reports may be committed.

## Measured on OGS-00011

`OGS-00011` is the Store epic #371 is about: a rebuilt Observed-Only Hybrid
Store Release with 3,169 Analyses, built by `opengwasdb` revision
`496bfe9cc9bbc85c60718d62aa4c6d43d83af810` (recorded in `records/build.json`).
The audit took 334.9 s wall and 7.1 GiB peak RSS (`resources.max_rss_kb`
7,494,776) on a 224-core host, read 22,699 files without changing one
(`unchanged.method: size+mtime_ns`), and reconciled:

- 114,514,113 shared-axis variants = 13,415,936 Dense-axis + 101,098,177
  off-panel, all three against the recorded counts;
- 2,908,357,117 Overflow associations, the recorded total, with 0 on the
  Dense-axis side;
- **101,098,177 off-panel entries observed and 0 unobserved** -- every off-panel
  axis entry carries at least one surviving observed association;
- 0 duplicate ALIDs over 114,514,113 distinct canonical identities, 2,526,751
  multiallelic sites covering 6,002,327 rows, and 1,273,627 rows with no rsid;
- 29,789,554 of the 101,098,177 off-panel entries (29.5%) carry exactly one
  association, the single-observer tail the epic's later tickets attribute;
- a construction reference of 13,415,936 ALIDs (`sha256
  7c7ab7bd…`) whose members are exactly the Dense Component's rows: 0 missing
  from the axis, 0 Dense-axis rows absent from it.

The run's `store.stable_view` shows why it still judged the view stable while a
`workflow/release.py OGS-00011 --config force=1` run from another epic held this
release's lock the whole time. That lock is a `finding` (`release_lock_held`),
not an `error`: the Store directory's device and inode were identical before and
after and every file was unchanged, and a swap replaces the directory, which is
what the identity comparison is for. A release whose lock outlives its run is an
operational fact about that release; withholding a reconciliation that measured
the Store twice and found it the same would hide it rather than report it.

The number to carry forward is the occupancy: the off-panel axis is not a tail
of unused pre-filter identities. Something admitted 101,098,177 distinct
variants and observed every one of them, which points the epic's remaining
hypotheses at admission and at source frequencies, not at axis bookkeeping.

### Attribution

`attribute` ran on the same Store in 454.8 s wall and 2.4 GiB peak RSS
(`resources.max_rss_kb` 2,497,492), reading the 2,908,357,117-row Ragged body
once (3,177 bands) and the 114,514,113-row Store Variant Table once, and
writing nothing into the Store. Its evidence is
`evidence/373/attribution.json` and `evidence/373/sample.tsv`. That run wrote
the seeded sample `trace` read; `attribute` no longer writes one (#428), and the
sample paragraphs below are a record of that run.

- **The attributed union is the census**: 101,098,177 off-panel variants over
  2,908,357,117 associations, equal to the axis complement and to the recorded
  `n_off_panel`, with 0 entries no Analysis observes.
- **29,789,554 variants (29.5%) are exclusive to one Analysis** -- one observer,
  one association -- and **66,659,169 (65.9%) are exclusive to one publication**,
  of which 36,869,615 are observed by two or more Analyses of that one
  publication. 34,439,008 (34.1%) span two or more publications.
- **Support is not a long tail.** Across Analyses: 29.5% observed by exactly 1,
  32.8% by 2, 34.3% by 3-100 and 4.6% by 101+. Across publications: 65.9% by 1,
  8.7% by 2, 24.3% by 3-100 and 1.1% by 101+. The union is dominated by a few
  very wide contributors rather than assembled from single observations. (The
  first run of this measurement, before the review's publication-deduplication
  fix, put 8,706,658 variants at 2 publications, 13,666,971 at 11-100 and
  1,124,859 at 101+; visiting Analyses in index order let one publication's
  stamp sit between two Analyses of another, and 378,865 variants were counted a
  bucket or more higher than they belong. The count with exactly one publication
  was unaffected, which is why only the 2+ buckets moved.)
- **Two Barnes DR Analyses are half of it.** Analysis 2398 (`GCST90455660`,
  PMID 41266372) holds 44,591,168 variants (44.1% of the union) and 8,848,991
  of them exclusively; 2397 (`GCST90455659`) holds 43,642,474. Their union is
  54,516,575 variants, 53.9% of the union, against 88.2M associations between
  them -- they overlap by 33.7M variants. The top 5 Analyses' union is
  79,851,489 (79.0%); the top 5 publications' is 83,876,527 (83.0%), with
  publication 41266372 alone at 54,617,412 (54.0%).
- **The union is 92.2% SNPs**: 93,251,288 `snp`, 7,840,090 `indel` and 6,799
  `complex` variants, with 1,431,605 positions carrying more than one off-panel
  row (2,948,117 rows). Indels are 7.8% of the union, which is not a tail of
  malformed rows.

The attribution answers "who", which is what makes #374's question askable: the
contributors are few, named, and their rows are reachable through the sample's
`source_row_lookup_hint`. That run's sample (`seed 42`) holds 278 rows: 20 for
each of the five leaders by exclusive variants (Analyses 2398, 2397, 1688, 3069
and 194), 20 for each of the five leaders by exclusive variants among
publications (PMIDs 41266372, 37872160, 34059833, 41233554, 41792138), 20
off-panel background rows, 20 shared-across-publications rows, and 58 on-panel
control cells for the Analyses 2398, 2397, 1688, 3069 and 194. It is 220 SNPs
and 58 indels and no `complex` row: complex variants are 6,799 of the union
(0.007%), so a 160-candidate pool holds one only by luck, and
`sample.allele_classes` makes that absence visible instead of leaving a reader to
assume the class was sampled. Every hint was read back during the run and again
independently afterwards: 278 of 278 rows resolve to the row they name, and every
row's `analysis_id` and `publication_pmid` match `analyses.tsv` at that
`analysis_index`.

Two facts the controls surfaced. Three of the 20 sampled panel variants are held
by no Analysis at all -- their `dense/z` row is entirely `Z_MISSING` -- so they
are skipped and counted (`panel_variants_skipped`) rather than emitted with a
blank owner, and a 40-row spot check found 3 of 40 Dense rows in that state
(about 7.5% of the 13.4M panel variants). Five control rows name the
lowest-indexed Analysis holding the cell because none of the sample's
contributors does. Both are properties of the Store's panel, not of this
sample.

### Trace

*`trace` has been removed (#428): this is the record of its one run on
OGS-00011, not a command to rerun. A build from opengwasdb 0.5.0 on writes the
same evidence, for every dropped row, in its Admission record.*

*Recorded at `opengwasdb` `5fc0766`, before the #331 admission amendment that
stores #380 repins to (`7308c6a`). Read every `maf_missing` below as "the rule
kept it, unjudged", and `associations_missing` as a retained tally: at the
pinned revision those rows are dropped instead, so a fresh trace of this Store
reports each one as `admission_disagrees_with_store` and exits 1. The measurements
are left as recorded; nothing here was recomputed under the new rule.*

`trace` ran on the same Store with the `seed 42` sample, the build manifest
`/data/opengwasdb/stores/OGS-00011/work/analyses.tsv` and 13 workers: 217.2 s
wall, 26 source files / 24.6 GB of gzipped harmonised GWAS-SSF read once each,
22,699 Store files unchanged (`size+mtime_ns`). Its evidence is
`evidence/374/trace.json`.

- **278 of 278 sampled rows located**, 0 errors, 0 unlocated. Every located row
  reproduces: its recorded physical line number holds the raw row whose tokens
  the report quotes. Spot-checked outside this tool with `zcat | sed -n`: line
  13599475 of `GCST90296333` is `4 26744568 A T -2.3888 24.2228 0.0 0.9214 ...`
  and line 523731 of `GCST90455659` is `1 14686671 C CG -1.6035 2.3351 0.0 0.4923`.
- **Every replay agrees with the Store**: 278 of 278 admissions, and 278 of 278
  stored-frequency codes. All 190 off-panel `maf_missing` rows carry the plane's
  `EAF_ABSENT` code (`-128`); all 30 off-panel `valid_common` rows carry a
  residual; all 58 control cells carry a frequency.
- **No sampled off-panel row was dropped for its MAF**, because #373 sampled the
  Store, not the source: 0 of 220 rows are `maf_below_threshold`. What the
  sample says about *why* each was admitted is the finding:

  | Source frequency class | off-panel rows (of 220) | controls (of 58) |
  |---|---|---|
  | `placeholder_zero` (`effect_allele_frequency = 0.0`) | 125 (56.8%, 95% CI 50.2-63.2) | 0 |
  | `placeholder_one` (`= 1.0`) | 24 (10.9%, CI 7.4-15.7) | 0 |
  | `missing` (blank/`NA`) | 41 (18.6%, CI 14.0-24.3) | 0 |
  | `valid_common` | 30 (13.6%, CI 9.7-18.8) | 58 (100%) |
  | `malformed`, `nonfinite`, `out_of_range`, `below_threshold` | 0 | 0 |

  So **149 of 220 off-panel sample rows (67.7%, CI 61.3-73.6) carry a literal
  `0.0` or `1.0`** and were retained as `maf_missing`, bypassing the 0.005 MAF
  filter entirely.
- **The leading contributors are the clearest cases.** Barnes DR (PMID 41266372):
  68 of 90 sampled rows are `0.0` (48 of 58 on `GCST90455659`, 20 of 20 in its
  exclusive stratum), the rest `valid_common`. Williams AT (`GCST90296333`): 24
  `0.0` and 1 `1.0` of 36. Chen J (PMID 34059833) is the other mechanism: 21 of
  21 rows are a blank/`NA` frequency, and `GCST90726344` 20 of 20 the same --
  absent rather than placeholder, and kept by the same branch of the rule.
- **The controls are the contrast the ticket asks for**: 58 of 58 are
  `valid_common` with a stored frequency, 0 placeholders. The admitted off-panel
  rows that do carry a frequency sit just above the filter (minimum MAF 0.0051,
  median 0.0145) against the controls' median 0.133 -- the signature of a hard
  cut that did apply wherever a frequency existed.
- **At population scale the Store attests the same branch.** Its own
  `provenance.maf` counts 11,018,053,834 rows dropped below the threshold and
  **192,435,653 rows retained with no usable frequency** (`associations_missing`),
  of which Barnes DR's two Analyses are 85,980,368. The 190 sampled off-panel
  rows are that bucket: 149 placeholders and 41 blanks.
- **The sample is not proportional.** 67.7% is the share of the *sampled*
  off-panel rows, not a weighted population share: the exclusive-contributor
  strata are deliberately contributor-heavy. The one stratum drawn from the whole
  union, `random_off_panel` (20 rows), is 12 placeholders and 8 `valid_common` --
  **60% (95% CI 36-81) placeholder, 40% (CI 19-64) genuinely common off-panel
  variants**. Neither number establishes a majority of the 101,098,177 off-panel
  variants; a full-union share, weighted by stratum, is #377's question.
- **The replay engine is not the recorded build engine.** The installed
  `opengwasdb` is `5fc0766` (version 0.3.0, from its `direct_url.json`); the
  Store's `records/build.json` records `496bfe9`. The report says so
  (`replay.agrees: false`, `replay_revision_differs`, a `finding`): parsing and
  admission here are the installed revision's semantics, and the reproducer pins
  the same rule in this engine. Installing the recorded revision and replaying
  under it is not something this command can do at run time.

The mechanism is established: at `5fc0766` `admit_rows` kept a row with no usable
frequency (`available == False` implies `maf_drop == False`), so a placeholder
`0.0` was admitted where a genuine `0.001` was dropped, the builder did exactly
that, the Store counted the rows it did it to, and the raw files carry the
tokens. #331 closed it at `7308c6a`: such a row is dropped, so a Store built
before it holds rows its own declared policy cannot admit -- the disagreement
stores #380's pin makes visible, and what a rebuild removes. What is *not*
established is the split of the 101,098,177 off-panel variants: on this sample
the placeholder share is 60% of the union-wide stratum with an interval that
includes 36%, and a material minority is legitimate admission of common variants
the EUR panel does not hold, which no admission fix would remove.
