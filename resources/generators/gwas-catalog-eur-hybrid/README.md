# gwas-catalog-eur-hybrid (Phase B)

Phase B entry point for the EBI GWAS Catalog `hybrid__European` pool: the
`gwas-catalog-ssf` Source Collection's European-ancestry Hybrid Store Family
(issue #150).

```text
inventory.py                freeze + preflight for this family's Source Inventory (issue #151)
config-full.yaml            the full-release Phase B configuration
derive_score_declarations.py  derive the reviewed score declarations from source headers (#176)
score-declarations.tsv      the committed derivation output (config-full.yaml consumes it)
```

## The full release (issue #150)

`config-full.yaml` declares the release this family is working toward: every
ready Analysis in the frozen Source Inventory at
`resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv`, resolved
through AF-based ancestry assignment and phenotype-SD effect-scale estimation.
It is Phase B only — it never declares how to build a Store.

```sh
# 0. (Optional) re-attempt non-ready rows first -- see "The rescue pass" below.
# 1. Freeze the inventory (writes resources/inventories/<snapshot-id>.{tsv,meta.yaml}).
pixi run inventory-freeze

# 2. Prove the frozen snapshot before spending hours on resolution.
pixi run preflight

# 3. Review the machine-readable report preflight wrote.
#    <work_root>/preflight/<snapshot-id>.json
```

Both commands default to `config-full.yaml`; pass `--config` to point them at a
different release configuration, `--cores` to lower the planned worker count,
`--inventory`/`--provenance` to preflight a different snapshot, or
`--snapshot-id`/`--out-dir`/`--candidates` to freeze a later one.

`inventory-freeze` needs the generated candidate table
(`resources/data/derived/store-candidates-analyses.tsv`), which is not tracked in
git: run `Rscript resources/scripts/ebi-studies.r` first, or pass `--candidates`
with this host's copy. Everything else it reads is either tracked or an
acquisition manifest under the mirror.

```sh
# A later snapshot, re-frozen after more acquisition, still accounting the same
# candidate pool. `--manifest` is repeatable and REPLACES the configured passes
# entirely; order is the precedence rule, so the last pass wins for any
# analysis_id it covers:
pixi run python resources/generators/gwas-catalog-eur-hybrid/inventory.py freeze \
  --snapshot-id gwas-catalog-ssf-eur-hybrid-2026-10-01 \
  --manifest base=/data/opengwasdb/raw/ebi-gwas-catalog/eur-hybrid-download-manifest.tsv \
  --manifest retry_transient=/data/opengwasdb/raw/ebi-gwas-catalog/eur-hybrid-download-retry-transient-manifest.tsv \
  --candidates resources/data/derived/store-candidates-analyses.tsv
```

Re-freezing does not change which Analyses an earlier snapshot selected; it
creates a new snapshot that the config must then be pointed at. `freeze` refuses
to run if a later pass would turn an already-ready Analysis into a non-ready one,
because that would silently drop a release member.

### The rescue pass (issue #151)

The `rescue_upstream_harmonised` pass re-attempted every row the 2026-09-10
snapshot recorded as non-ready. Re-run it with:

```sh
# The non-ready set is derived from the previous snapshot, so the input is
# reproducible from tracked data rather than hand-listed:
awk -F'\t' 'NR>1 && $6!="ok" && $6!="already_present"{print $1}' \
  resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv > /tmp/not-ready.txt

pixi run python resources/scripts/download-ebi-gwas-catalog-eur-hybrid.py \
  --accessions-file /tmp/not-ready.txt \
  --harmonised-index /data/opengwasdb/raw/ebi-gwas-catalog/harmonised_list-2026-09-22.txt \
  --manifest /data/opengwasdb/raw/ebi-gwas-catalog/eur-hybrid-rescue-upstream-harmonised-manifest.tsv
```

It is idempotent: files already in the mirror are not re-fetched, so a second
run reproduces the same manifest.

**Delta, 2026-09-10 -> 2026-09-22.** The candidate pool is unchanged at 6,035.
Ready rises 4,570 -> 4,783 (+213: 111 quantitative, 102 case-control, +122.6 GB
compressed). No Analysis lost readiness. Both snapshots stay in git so the
delta stays reviewable.

The rescue pass reclassified 213 rows to `already_present`:
- 33 accessions harmonised upstream since initial acquisition;
- 105 accessions publishing an `odds_ratio` effect column (read as `log(odds_ratio)`);
- 1 accession publishing `BETA` in uppercase (`GCST90044776`);
- 74 quantitative accessions publishing a signed `z_score` with per-row sample size and EAF.

The remaining non-ready rows are not transient failures:

| Status | n | Why |
| --- | --- | --- |
| `missing_remote_harmonised_yaml` | 1,104 | No harmonised GRCh38 file upstream. EBI's own `sumstats_harm_status` database records most as `cannot_harm`. |
| `header_rejected` | 98 | Downloaded, but no usable effect column: 95 have no effect column at all; 2 are case-control accessions reporting z-score only (`GCST007228`, `GCST90010719`), which cannot derive a log-OR; 1 is a quantitative z-score accession with no sample-size column (`GCST90134637`). |
| `data_absent_upstream` | 49 | A filename resolved, but upstream serves HTTP 404 for the association file — an orphan `-meta.yaml`, or a stale `harmonised_list.txt` entry. |
| `metadata_rejected` | 1 | Sidecar declares GRCh37. |

The earlier `data_failed` count was misleading: all 52 turned out to be
permanent absence, not a transfer that could be retried.

## Candidate generation (issue #153)

The Phase B candidate workflow turns the frozen Source Inventory into a
**candidate** Release Bundle (`stores/OGS-xxxxx/`, `status: candidate`). It runs
preflight, derives the canonical resolver manifest, invokes
`opengwasdb resolve-analyses`, accounts every resolver record, applies the
registry's membership/exclusion policy, validates the staged bundle and renames
it into place atomically. It never builds, validates or accepts a Store, and it
never invokes Phase A.

```sh
# Preflight + resolve + verify + finalise in one resumable run:
pixi run generate-candidate OGS-00011 \
  --config resources/generators/gwas-catalog-eur-hybrid/config-full.yaml \
  --cores 64 \
  --resume
```

The resolver owns the worker pool and the per-Analysis checkpoints, so
`--cores` is passed to `opengwasdb resolve-analyses` and a resumed run reuses
every successful record whose fingerprint still matches (source checksum/size,
tool revision, reference, extraction panel, gates and method tier). `--resume`
is safe to use after an interruption; without it, every record is recomputed.

### Stages

Pass `--stage <name>` to run one stage against the records already under the
work root (also how `workflow/generate.smk` wires the coarse DAG):

| Stage | What it proves / produces |
|---|---|
| `preflight` | The frozen inventory still matches the mirror, the declared Reference Resources exist, and every study design has a method tier. Writes `<work-root>/OGS-xxxxx/preflight/<snapshot>.json`. |
| `prepare` | The canonical resolver manifest from the frozen exact `data_file` paths and the per-design method tiers. |
| `resolve` | Invokes `opengwasdb resolve-analyses`; writes records, `index.json`, `resolve.log`, and the `resolution_receipt.json` that binds the run to its contract. |
| `verify` | Accounts every record and requires the current contract and every record digest to match the successful resolution receipt; refuses a missing, stale, duplicate, extra, or incompatible-schema record. |
| `emit` | Applies release policy, checks the pinned schema and `bundle.check()`, and atomically publishes `stores/OGS-xxxxx/`. |
| `all` | Default: every stage in order. |

Useful options: `--registry-root` (default `stores/`), `--work-root` (default
`output.work_root`), `--inventory` / `--provenance` / `--candidates` overrides,
and `--resume`.

A standalone `--stage verify` or `--stage emit` requires the
`resolution_receipt.json` a successful `resolve` wrote. The receipt binds the
resolver manifest, the declared gates / ancestry reference and fine-group map /
extraction panel / reference-AF resources, the installed resolver's content
identity, and every Analysis's fingerprint digest. Changing any of those (or a
source file) after resolving makes the receipt stale, and `verify`/`emit` fail
and tell you to re-resolve rather than freezing stale records into a candidate.
`--cores` and `--resume` do not change the contract or the bundle tables and
sidecars.

### Candidate output and controlled exclusions

```text
stores/OGS-xxxxx/
  release.yaml    build.yaml    analyses.tsv    validation.yaml
  sidecars/ source_readiness.tsv ancestry.tsv sd_estimation.tsv exclusions.tsv reference_overlap.tsv
```

* `analyses.tsv` holds every selected ready Analysis. Non-member rows stay in it
  with `exclude_from_build: true` and a controlled reason in `inclusion_reason`.
* Membership is the frozen inventory's; the candidate metadata table
  (`resources/data/derived/store-candidates-analyses.tsv`) is joined only for
  resolved labels, publication identity, total N and the case/control counts the
  OpenGWASDB schema needs for `log_or` rows.
* Issue #152's policy is encoded here: full-reference AF ancestry assignment;
  source-AF-only quantitative estimation; case-control rows on
  `log_or`/`binary_trait`; and controlled exclusions (with the reason recorded in
  `sidecars/exclusions.tsv`) for non-target or unassigned ancestry, EAF
  orientation failures, unusable source AF, incomplete metadata, ordinary
  resolution failures, and a successful record whose own tally of build-eligible
  rows is zero (`no_build_eligible_rows`: no row has a finite effect and a
  positive standard error), Analyses on the reviewed placeholder-effect list
  (`effect_placeholder_rows`, see below), and -- when
  `store_composition.require_maf_filtered` is on -- otherwise admissible
  Analyses with no applied MAF floor (`not_maf_filtered`, see below).
* Duplicate-content accessions (`GCST90565871`/`GCST90565872` and
  `GCST90624704`/`GCST90624705`) are surfaced in `sidecars/source_readiness.tsv`
  and a warning; they are never silently collapsed.

### Reference-overlap gate (#174)

`sidecars/reference_overlap.tsv` and the `validation.yaml` `reference_overlap`
block measure how much of an included Analysis's source the declared variant
reference covers, and the gate projects off-reference overflow before a Hybrid
build commits to it. When a resolver record carries the post-INFO
`build_eligible_rows` diagnostics, the denominator is the Analysis's
build-eligible rows and the sidecar reports `build_eligible_rows`,
`build_eligible_rows_on_variant_reference`,
`build_eligible_rows_off_variant_reference` and `build_eligible_rate`. The gate
falls back to the legacy pre-INFO `rows_scanned` /
`variant_reference_rows_matched` columns only for a record that does not carry
the new diagnostics; the legacy columns are always kept so an older record is
still reviewable. The summary names the denominator it used in
`projected_off_reference_basis` (`build_eligible_rows`, `legacy_rows_read`, or
`mixed`). A carried-but-unusable count (an invalid or inconsistent on/off split,
or an absent count with no usable legacy fallback) is treated as a missing
measurement, never as a zero off-axis share. An *explicit integer zero* is not a
measurement at all: the membership policy excludes such an Analysis up front as
`no_build_eligible_rows` (#176), so it is never reported as a missing overlap and
never refuses the candidate.

### INFO threshold contract and core integration (#175)

`defaults.info_score_threshold` requests a finite floor in `[0,1]` (default
`0.6`); explicit `0` disables filtering. Candidate `analyses.tsv` emits a
numeric threshold only on per-Analysis resolver evidence; every other Analysis
emits literal `NaN` and empty declaration cells, even with an explicit zero
request. The frequency a MAF column carries is not an imputation score, and a
header that merely resembles `INFO` is not a declaration either: neither the
reader's `effect_allele_frequency` nor a look-alike name becomes INFO. An
Analysis with no resolver evidence is reported as unavailable, not as filtered
or quality-passing. Legacy bundles without the column remain valid.

An optional `source.imputation_score_declarations` path may point to an
explicit, reviewed TSV (relative to the repository root or absolute). It must
have **exactly** these headers in order:

```text
analysis_id\timputation_score_column\timputation_score_kind\timputation_score_provenance
```

Each `analysis_id` must occur in the frozen inventory at most once. The column
name is the exact, case-sensitive source header (no normalization); the kind is
`imputation_info` or `imputation_r2`; provenance must cite independent provider
semantic evidence (e.g. a provider data dictionary), not a header guess or
MAF/EAF. Unknown IDs, duplicate IDs, malformed rows and unsupported kinds fail
prepare. `config-full.yaml` sets this path to the committed
`score-declarations.tsv` (derived below). An Analysis absent from the table
passes literal `NaN` and empty declaration columns to the resolver. A mapped
Analysis passes the requested numeric threshold (including `0`) and the exact
three fields to the resolver manifest. The manifest checksum, declaration file
checksum and score fields in the resolver's per-Analysis fingerprint bind
verify/emit to the reviewed input; a resolver that does not fingerprint a
mapped declaration fails verification. Changing the mapping requires re-resolve.
A source header that happens to resemble INFO/R2 or a MAF column is never
automatic evidence. Without this optional input, legacy configuration works.

**Emission rule.** For a mapped Analysis, finalisation emits the requested
numeric threshold plus the exact declaration triple only when **all** of these
hold:

1. the resolver record's `diagnostics.info_score_state` is `disabled` (explicit
   zero) or `filtered` (positive floor) -- the two post-INFO states core writes;
2. `diagnostics.info_rows_usable > 0`; and
3. the record's per-Analysis fingerprint still binds the exact
   `info_score_threshold` and declaration triple from the manifest.

Every other Analysis -- undeclared, a legacy record without the #175
diagnostics, or a mapped Analysis whose evidence is unusable -- emits literal
`NaN` and empty triple cells. A mapped Analysis with zero usable scores is
*included*: core no longer reports it as a controlled failure (#176), and the
record's `info_score_state = "no_usable_scores"` is read as no evidence, so the
Analysis stays in the bundle with literal `NaN` INFO cells. The capability-wide
`VALIDATED_INFO_SCORE_READERS` allowlist is retired: only the per-Analysis
record evidence decides, because a reader capability cannot prove an
individual source's declared score is valid, and the manifest declaration
alone is not evidence.

`analyses.tsv` therefore carries four optional INFO columns: the requested
`info_score_threshold` plus `imputation_score_column`,
`imputation_score_kind` and `imputation_score_provenance`. `bundle.check()`
rejects a numeric threshold without the complete triple and a triple without a
numeric threshold.

### Deriving the score declarations (#176)

`config-full.yaml` sets `source.imputation_score_declarations` to the committed
`score-declarations.tsv`, which `derive_score_declarations.py` derives
deterministically from the header of every ready inventory row:

```sh
pixi run python resources/generators/gwas-catalog-eur-hybrid/derive_score_declarations.py
```

The synonym table is fixed and case-sensitive (the OGS-00011 admission
contract, #176), first match in the listed order wins, and look-alikes
(`additional_info`, `lowQuality`, z-scores, `r2_iCOGS`,
`mmm_var_info_nonmissing`, ...) are never declared. A header with no synonym is
absent from the output; the candidate then emits literal `NaN` for it. Re-run
the command and commit the result when the frozen inventory changes, rather
than hand-editing the TSV.

### MAF threshold contract (#176)

`defaults.maf_threshold` requests a per-Analysis MAF floor in `[0, 0.5]`;
`0` disables filtering and omitting the key means no MAF filter at all. The
frozen inventory's `yaml_file` metadata decides the per-Analysis exemption: an
Analysis is exempt (emitting literal `NaN`) only when its
`genotyping_technology` list is non-empty and **every** technology appears in
`source.maf_filter_exempt_genotyping_technologies`; missing technology metadata
is never an exemption. `config-full.yaml` sets `maf_threshold: 0.005` and
exempts `Whole genome sequencing` and `Exome-wide sequencing` (operator
decision, #176): the 158 sequencing-only Analyses carry no MAF floor because
their low-frequency calls are observed rather than imputed, while an Analysis
that also lists a genotyping array is filtered.

An optional `source.maf_filter_exempt_analyses` path may point to a reviewed TSV
(relative to the repository root or absolute) that exempts individual Analyses
with a recorded reason. It must have **exactly** these headers in order:

```text
analysis_id\treason
```

Each `analysis_id` must be non-empty and occur at most once, and `reason` must
be non-empty; a missing file, a different header, a blank id and a duplicate id
all fail configuration with the file and line. `config-full.yaml` sets this path
to the committed `maf-filter-exemptions.tsv`, which exempts `GCST90428462` and
`GCST90428463`: every row of both carries `effect_allele_frequency = 0.0`, a
placeholder the study metadata's own `minor_allele_freq_lower_limit` (0.01) says
is not a real frequency, so core reads the frequency as missing and a MAF floor
would compare against a fabricated zero (operator decision, #176, 2026-09-29).
An exempt Analysis emits the same literal `NaN` as an exempt technology; the
resolution receipt contract records the exemption file and its
`analysis_id -> reason` mapping, so changing either makes the receipt stale and
requires a re-resolve.

An optional `source.effect_placeholder_exclusions` path points to a reviewed
TSV of the same shape and validation (`analysis_id`, `reason`). Each listed
Analysis is excluded at emit as `effect_placeholder_rows` (category
`effect_scale`), with its `reason` as the exclusion detail. It exists for
sources that write an effect they did not estimate as
`±2.2250738585072014e-308` (the smallest normal double, so only the sign
survives) with `standard_error` 0. Core derives a missing standard error from
the effect and the p-value (opengwasdb#236) and refuses only an effect of exactly
0, so such a row would be stored as an effect of ~0 with a standard error of
~1e-308. `config-full.yaml` lists the 27 included OGS-00011 Analyses that carry
at least one such row in a full-file scan (2026-09-30). Four are almost entirely
placeholder (`GCST90454200/1`, whose SD then estimates as exactly 0, and
`GCST90565871/2`); the other 23 have fewer than 0.5% of rows affected. This is
an emit-time membership decision; it does not change the resolver manifest or
receipt. Remove the table once core refuses the placeholder itself.

Independently, a quantitative Analysis whose phenotype-SD estimate is not a
positive finite number is excluded as `sd_no_qualifying_evidence`: the builder
refuses a non-positive `original_sd`.

The resolver manifest carries the derived `maf_threshold` per Analysis.
Candidate `analyses.tsv` emits the numeric value only on resolver evidence: the
record's `diagnostics.maf_state` is `disabled` or `filtered` **and** the
per-Analysis fingerprint still binds the same `maf_threshold`. Every other
Analysis emits literal `NaN`. `verify` requires a numeric request to be bound
by the fingerprint, exactly as it does for the INFO threshold, and the
resolution receipt's contract records the configured default and both exemption
rules, so changing any of them makes the receipt stale. `bundle.check()` rejects any
`maf_threshold` value that is not literal `NaN` or a finite number in
`[0, 0.5]`.

### Store composition (#203)

`store_composition.require_maf_filtered` is an optional Phase B generator-config
rule deciding whether a shared Store may admit an Analysis with no applied MAF
floor. Absent or `false`, membership is unchanged; `true` requires a positive
`defaults.maf_threshold` and adds one gate after every other membership decision:
an otherwise admissible Analysis whose emitted `maf_threshold` is literal `NaN`
is excluded as `not_maf_filtered` (category `store_composition`), and its
exclusion detail records the requested floor, the resolver's `maf_state`, the
source's `genotyping_technology`, and how many of its build-eligible rows are off
the variant reference. An Analysis excluded for any other reason keeps that
reason.

The rule exists because the 148 included Analyses the sequencing exemption
leaves unfiltered in `config-full.yaml` (97 WGS and 51 WES) carry 49,537,183
Overflow variants -- 32.9 % of `OGS-00011`'s Overflow -- on 63,770,673 rows, and
49.3 M of the axis's 75.9 M variants below 0.5 % MAF are carried only by them.
Build and validation scale with that axis (opengwasdb #254), even though the
Store opens in about 0.29 GiB (opengwasdb #252). Issue #203 called these Analyses
"no-EAF", but they report EAF: every one passed the EAF-orientation check, 126
had their phenotype SD estimated from source MAF, and a sampled WGS source
(`GCST90446475`) carries `effect_allele_frequency` on every one of its first 2 M
rows, 81 % of them below 0.5 % MAF. They are exempt from the floor, not
unfilterable, so the rule is worded as "rows were MAF-filtered" rather than "the
source reports EAF". See
[ADR 0033](../../../docs/adr/0033-shared-stores-admit-only-maf-filtered-analyses.md).

The gate is an emit-time membership decision, like `effect_placeholder_rows`: it
does not change the resolver manifest or the resolution receipt, so toggling it
needs only `--stage emit` and never a re-resolve. Every candidate
`validation.yaml` gains a top-level `store_composition` block recording whether
the rule ran, the configured floor, how many included Analyses have no applied
floor and how many were routed, and the off-reference row sums for each (a sum
is `null` when no `variant-reference` is declared or any counted Analysis lacks
the resolver count). Two warnings become `Review:` lines: one when Analyses were
routed as `not_maf_filtered`, and -- when the rule is off but a floor is
configured -- one when included Analyses have no applied floor, so a later
release cannot silently re-inflate the axis. With the rule on, `release.yaml` notes state the composition the Store
admits.

The 148 routed Analyses need their own sequencing Store, a separate Store
Release not yet made (follow-up work).

### Human review before acceptance

A successful run leaves `status: candidate`. Nothing in this workflow accepts,
registers or builds it. A human reviewer should:

1. read `release.yaml` (the frozen inventory checksum, the executed resolver
   argv, the inclusion/exclusion counts) and `validation.yaml`;
2. review every row of `sidecars/exclusions.tsv` and decide whether each reason
   is acceptable for this release (a non-`EUR` assignment or an orientation
   failure is expected evidence, not necessarily a defect);
3. resolve the duplicate-content groups in `sidecars/source_readiness.tsv`;
4. inspect the dense-axis / `--variant-reference` choice for the Hybrid Recipe,
   which `config-full.yaml` deliberately leaves undeclared (see below);
5. only then accept the bundle, set `accepted_at`, and let Phase A build it.

A generated candidate is a reviewed git change like any other: commit
`stores/OGS-xxxxx/`, regenerate the master list (`pixi run index`) so
`stores.tsv`/`STORES.md` include it, and open a pull request. `index` also writes
the bundle's generated `summary.yaml`, so `bundle-check` and the CI clean-tree
gate stay green.

`config-full.yaml`'s `build` block declares the reader capability and assembly
but no `variant-reference`/`reference-panel`: the full release's Dense-Component
axis is a Reference Resource decision for acceptance, and inventing a host path
would bind the candidate to one machine. The candidate is schema-valid and
plannable without it; acceptance may add one before the Store is built.

## Settled Decisions (issue #152)

Both Reference Resource decisions required before Phase B candidate generation (issue #153)
are finalized and ratified on empirical concordance evidence:

1. **Ancestry Extraction Method:** Retain Method A (full reference scan `ref_freqs.hg38.tsv.gz`,
   `extraction_panel: null`). `qc-panel-hg38` was evaluated on the preregistered 106-Analysis sample
   and rejected due to failing the genome-wide concordance gate (95.1% < 98.0% threshold) caused
   by overlap drop on non-standard arrays. Full evidence is in `docs/qc-panel-concordance-report.md`.
2. **Reference-AF Fallback Policy:** Adopt Option 3 (**Source-AF-Only**). `config-full.yaml` sets
   `effect_scale_validation.reference_resources: []`. Quantitative Analyses lacking usable source AF
   receive an explicit `skipped` resolution (`no_reference_resource_for_ancestry`) and are excluded from
   candidate release membership rather than estimated against an absent panel.

The earlier ten-Analysis pilots live in this directory as
`config-pilot-10.yaml` (case-control) and `config-quant-pilot-10.yaml`
(quantitative), and their Release Bundles are `stores/OGS-00004` and
`stores/OGS-00005`.

## QC Panel Concordance Study (issue #152)

The study was preregistered at `docs/spec/qc-panel-concordance-preregistration.md`
and the ratified final decision report is at `docs/qc-panel-concordance-report.md`.

```sh
# 1. Generate / re-generate the frozen stratified sample manifest (106 Analyses):
pixi run concordance-sample

# 2. Run the concordance study in a Herdr pane using up to 64 cores:
pixi run --environment dev concordance-run --cores 64 \
  --out-dir /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance

# 3. Review the generated comparison artifacts:
#    - /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_results.json
#    - /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_comparison.tsv
#    - /data/opengwasdb/work/gwas-catalog-eur-hybrid/concordance/concordance_report.md
```

### Reference-AF Fallback Policy

This release adopts an explicit **Source-AF-Only** policy:
- `config-full.yaml` sets `effect_scale_validation.reference_resources: []`.
- Quantitative Analyses lacking usable source AF receive an explicit `skipped`
  resolution with reason `no_reference_resource_for_ancestry` and are excluded
  from the candidate manifest.
- The absent panel `/data/opengwasdb/reference/ukb-hg38` is never treated as usable evidence.

## Dense per-ancestry acquisition (PMID 39024449)

The same harmonised-file downloader also acquires the four single-publication
dense stores split out of PMID 39024449 (Verma et al. 2024, VA Million Veteran
Program), planned as OGS-00012..OGS-00015. The candidate table already
partitions that publication into per-ancestry `dense__pmid-39024449__*` store
keys, so acquisition is one pass per key against this same mirror:

```sh
resources/scripts/download-ebi-gwas-catalog-dense-pmid39024449.sh
```

The wrapper loops the four keys (`European`, `African`,
`Hispanic-or-Latin-American`, `East-Asian`), writes
`dense-pmid39024449-<ancestry>-download-manifest.tsv` beside the eur-hybrid
manifests, and re-runs each key so a 404 caused by EBI load is re-attempted
rather than frozen as absent.

**Raw fallback.** Many of this study's accessions are in EBI's
`harmonised_list.txt` but publish only the raw `<GCST>.tsv.gz` (GRCh38
GWAS-SSF v1.0, `is_harmonised: false`) -- the harmonised `<GCST>.h.tsv.gz`
directory genuinely does not exist. For those the harmonised pass records
`missing_remote_harmonised_yaml`/`data_absent_upstream`, and a second pass
(`resources/scripts/download-ebi-gwas-catalog-raw.py`) then downloads the raw
pair into the same mirror layout, writing
`dense-pmid39024449-<ancestry>-raw-download-manifest.tsv`. It skips every row
the harmonised pass already obtained, so nothing is fetched twice, and it
records the raw `genome_assembly`/`file_type`/`is_harmonised` fields so a later
harmonisation-migration step has the provenance it needs. The raw pass is
acquisition only: which pass supplies membership is still a Phase B decision.

## Pieces

- `resources/inventories/` — the frozen snapshot, sample manifest, and what their columns mean;
- `resources/generators/lib/source_inventory.py` — the freeze merge rule, the
  readiness vocabulary, and preflight;
- `resources/generators/lib/concordance_sampling.py` — deterministic stratified sampling;
- `resources/generators/lib/qc_panel_concordance.py` — concordance comparison engine & metrics;
- `docs/spec/qc-panel-concordance-preregistration.md` — locked preregistration document;
- `docs/qc-panel-concordance-report.md` — finalized decision & empirical concordance evidence report;
- `resources/scripts/download-ebi-gwas-catalog-eur-hybrid.py` — acquisition,
  which writes the status manifests the freeze merges;
- `resources/scripts/download-ebi-gwas-catalog-raw.py` — raw-file fallback for
  accessions EBI did not harmonise, writing a separate status manifest.
