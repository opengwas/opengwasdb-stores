# gwas-catalog-eur-hybrid (Phase B)

Phase B entry point for the EBI GWAS Catalog `hybrid__European` pool: the
`gwas-catalog-ssf` Source Collection's European-ancestry Hybrid Store Family
(issue #150).

```text
inventory.py       freeze + preflight for this family's Source Inventory (issue #151)
config-full.yaml   the full-release Phase B configuration
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
  sidecars/ source_readiness.tsv ancestry.tsv sd_estimation.tsv exclusions.tsv
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
  orientation failures, unusable source AF, incomplete metadata and ordinary
  resolution failures.
* Duplicate-content accessions (`GCST90565871`/`GCST90565872` and
  `GCST90624704`/`GCST90624705`) are surfaced in `sidecars/source_readiness.tsv`
  and a warning; they are never silently collapsed.

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

## Pieces

- `resources/inventories/` — the frozen snapshot, sample manifest, and what their columns mean;
- `resources/generators/lib/source_inventory.py` — the freeze merge rule, the
  readiness vocabulary, and preflight;
- `resources/generators/lib/concordance_sampling.py` — deterministic stratified sampling;
- `resources/generators/lib/qc_panel_concordance.py` — concordance comparison engine & metrics;
- `docs/spec/qc-panel-concordance-preregistration.md` — locked preregistration document;
- `docs/qc-panel-concordance-report.md` — finalized decision & empirical concordance evidence report;
- `resources/scripts/download-ebi-gwas-catalog-eur-hybrid.py` — acquisition,
  which writes the status manifests the freeze merges.
