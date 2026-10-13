# MVP metric-resolution plan (PMID 39024449) — planning checkpoint

**Status:** plan for parent review. **Not** an operational GO. No resolver run,
no association-body open, no Store build, no scientific acceptance.
**Ticket:** #221 (planning follow-up). **Prepared against** `dev@2075cbf`.
Supersedes the first plan at `ba0f2e1`; that history is preserved.

## 1. Scope and non-goals

Resolve the pending MVP candidate Analytical Metadata (Assigned Ancestry,
phenotype SD, effect scale, source-AF/SE orientation evidence) through the
**public OpenGWASDB resolver interfaces only**, in a bounded representative
pilot, subject to a **new read-scope authorization** (§6).

Non-goals: no full 6,022 resolution, no Store build, no acceptance, no ID
activation, no FinnGen renumbering, no AMR LD production, no source-body
hashing, no separate SD re-scan of any source.

## 2. Frozen inputs and honest state

| Input | Path / identity |
|---|---|
| Metadata root (frozen) | `/data/opengwasdb/work/epic232/220/metadata-refresh-parent-baf141c-20261013T123913Z` |
| Content hash (recomputed) | `1f14b88c2c383d8bac97215a8807503ef600fed2d2a019067e9962777438310f` over 12,049 entries |
| dbGaP mapping | `.../conversion-retry-20261012T223021Z/mapping.tsv` (6,022 bound rows) |
| Candidate table | `resources/data/derived/store-candidates-analyses.tsv` (primary checkout) |
| Core runtime | `opengwasdb` 0.3.0 @ `4e922566d7fa4c13a11be3637069da63e48be28c` (verified 218 env) |
| Source assembly | GRCh38 (native; no liftover) |
| Owner decisions | `resources/scripts/dbgap-phs002453-key-discrepancies.yaml` (D1/D2/traits; dbGaP authoritative) |

**State is tracked definitions only.** There is **no real emitted candidate
Release Bundle**: `resources/generators/dbgap-mvp/candidates.yaml` is a tracked
plan/definition view, and `generate_candidate.py emit` has never been run. No
`release.yaml:source_snapshot` exists yet; the planned emit will bind the
metadata content hash `1f14…`.

## 3. Public core interfaces (verbatim, no invented flags)

- **`opengwasdb resolve-analyses MANIFEST RECORDS_DIR`** — one-pass resolution to
  `RECORDS_DIR/<analysis_id>.json` + deterministic `index.json`. It has **no
  `--af-source` flag**: the source-MAF versus reference-MAF SD tier is selected
  per Analysis by that row's declared `original_sd_method` (the resolver reads
  the `se`/`af`/`beta` evidence that method needs).
  Reference/gate flags:
  `--ancestry-reference /data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz`
  (494,105,972 bytes),
  `--ancestry-groups /data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv`
  (385 bytes),
  `--extraction-panel resources/reference-resources/qc-panel-hg38/qc_panel.tsv`
  (420,582 bytes),
  `--default-source-reader-capability opengwasdb.gwas-ssf`,
  `--maf-floor 0.01 --tau 0.5 --delta 0.2 --n-min 5000 --residual-max 0.06 --orientation-flip-r -0.5`
  (the CLI defaults, restated explicitly),
  `--reference-version ukb-ancestry-mixture-hg38-prive2022-grch38`,
  `--evidence-sample 20000`, `--max-ancestry-sites 50000` (bounds **ancestry
  evidence only**; a quantitative Analysis still reads to EOF for SD evidence),
  `--max-rows 200000` (exact per-source scan cap for the pilot),
  `--n-workers 2 --resume --largest-first`.
- **`opengwasdb estimate-phenotype-sd MANIFEST OUT`** — focused SD re-run if
  needed; it does take `--af-source source|reference` (this is the SD command,
  not the resolver). Not used in the pilot.
- Python API for the registry adapter: `resolve_analyses_manifest(...)` with the
  same arguments; the registry implements no statistics (ADR 0017).

**Fingerprint is inherited, not body-hashed (public read code).** In
`opengwasdb.build.resolve_manifest`, the fingerprint records
`source_recorded_sha256` from the manifest's `checksum`/`sha256` column and
`source_file_size`/`source_file_mtime_ns` from `_stat_source_file` (a `stat()`
call), then `compute_fingerprint_digest`. It does **not** hash the association
body. `compute_file_sha256` is used only for the ancestry reference, groups,
extraction panel and (if declared) variant reference.

## 4. Representative pilot selection (metadata only, reproducible)

Selection rule: per ancestry bucket, sort `analysis_id`; take the first two
`quantitative` and the first `case-control`; fill from the next sorted row if a
design is absent. 15 rows:

| Bucket | Quantitative | Case-control |
|---|---|---|
| EUR | GCST90475091, GCST90475094 | GCST90475169 |
| AFR | GCST90475090, GCST90475093 | GCST90475184 |
| AMR | GCST90475089, GCST90475092 | GCST90475183 |
| EAS | GCST90475346, GCST90475350 | GCST90475203 |
| META | GCST90479500, GCST90479501 | GCST90479527 |

The selection is written as a **metadata-only** TSV (no core call, no body open)
with per-row binding evidence, not summarised labels: `analysis_id`,
`mapping_ancestry`, `study_design`, `mapping_sample_size`, `n_cases`,
`n_controls`, `source_file_path` (the metadata-root symlink), `source_file_size`
(target `stat`), `source_genome_build` (sidecar), `sidecar_gwas_id`,
`sidecar_data_file_name`, `metadata_content_sha256` (`1f14…`). Output path:
`/data/opengwasdb/work/epic232/221/metric-pilot-1f14b88c-20261013T123913Z/selection/selection.tsv`
(fresh, outside any data root, exclusive pre-write guard).

## 5. Metric criteria and null/pending treatment

- **Assigned Ancestry:** core gates only; `unassigned` and `gate_reason`
  recorded verbatim, never coerced.
- **Effect scale is source-provenance, not a study-design default.** A
  quantitative Analysis is **not** automatically `sd`. The registry reads the
  source's declared effect semantics (a per-allele/per-unit `beta`+`se` is not
  by itself a standardized effect) and records the public
  `StoredEffectScale`/`original_effect_scale` value only when the source
  provenance supports it (`sd` with a standardized/raw-unit declaration,
  `log_or`, `log_hazard`). Unknown or ambiguous provenance stays **pending**;
  it is never defaulted to `sd` by the core or the registry.
- **Phenotype SD:** `original_sd_method` from the core tiers; `unavailable`
  stays pending (blank SD, no default). A binary trait is `binary_trait`, never
  an SD. A value is promoted only when the method is a real estimated/provided
  tier.
- **Source AF/SE and orientation:** `eaf_orientation`/`eaf_orientation_r`
  recorded; an orientation failure is a controlled failure/pending, never a
  loosened threshold. `standard_error` is exactly what the reader reports
  (`#NA`/absent stays absent).
- **Failures:** core `RecordStatus` `success` / `controlled_failure` / `skip`;
  only `success` with a real method tier counts as resolved. A pilot run bounded
  by `--max-rows` is a **capped diagnostic**: its SD evidence is marked
  `source_evidence_scope: capped_pilot_diagnostic`, and `RecordStatus=success`
  alone does **not** promote a capped estimate to whole-source qualification.

## 6. Read-scope change request (blocker before any run)

The existing owner authority for #220 covers **only two conversion inputs**
(`GCST90476298`, `GCST90481129`) plus metadata, and forbids reads of the other
6,020 association bodies. All 15 proposed pilot rows are **outside** that scope,
so parent start requires **new owner/operator authorization**:

- read the 15 named association bodies, one bounded pass each;
- load the ancestry reference `ref_freqs.hg38.tsv.gz` (494,105,972 bytes), the
  `ancestry_groups.tsv` map (385 bytes) and the tracked `qc_panel.tsv`
  (420,582 bytes);
- **no** source-body hashing or checksum recomputation (the resolver inherits
  the recorded SHA/stat, §3), and **no** separate SD re-scan of any source;
- no genotype/LD/panel build, no Store operation.

This request is not an expansion of the old scope by the parent; it is an
explicit new authorization to be granted by the owner/operator.

## 7. META ancestry semantics

The core always infers a dominant super-population mixture per Analysis and has
**no flag to skip inference**. The registry therefore keeps the core record
**immutable** (its inferred evidence stays as provenance in the receipt) and
canonicalises the META bundle's `assigned_ancestry` to **empty** by explicit
policy. This is a registry policy decision, not a promise that the resolver will
not assign an ancestry; nothing rewrites the core record. META remains
observed-only with no completion.

## 8. AMR classifications and no LD production

The AMR LD calibration pilot is **already approved** (ticket #241, MAF 1%); this
plan does not re-ratify that proposal. The resolver may assign AMR or leave it
unassigned. **No AMR LD production** is assumed: the AMR completed definition
stays a pending, blocked plan until a production qualification exists.

## 9. Resource budget, outputs and provenance

- ≤ **2 resolver workers**, ≤ **4 CPU**, BLAS/OpenMP threads **1**.
- Pilot stop wrapper (executable, not a placeholder): fail and record if total
  wall time exceeds **1800 s** or peak RSS exceeds **4096 MB**, or if any single
  Association read exceeds its `--max-rows` cap.
- Outputs (fresh isolated root
  `/data/opengwasdb/work/epic232/221/metric-pilot-1f14b88c-20261013T123913Z/`):
  `selection/selection.tsv`, `records/<analysis_id>.json`, `index.json`,
  `receipt/resolution-receipt.json`. The receipt records the core commit
  `4e922566`, the metadata content hash `1f14…`, the exact reference paths and
  the resolver-computed `anc_ref_sha256`/`anc_grp_sha256`/`panel_sha256`, the
  gate flags, per-row `RecordStatus`/`original_sd_method`/`stored_effect_scale`/
  `source_evidence_scope`, and the measured wall/RSS. All writes use the shared
  `resources/generators/lib/safe_write.py` exclusive-temp, containment-checked
  pattern; no path is pre-existing.

## 10. Acceptance and stop conditions

- **Pilot accepted only if:** all 15 rows are `success` with a real SD/effect
  tier, or an explicitly classified `controlled_failure`/`skip`; ancestry/SD/
  orientation outcomes are internally consistent; measured resources stay inside
  §9; the metadata content hash still equals `1f14…`.
- **Stop and report** on any budget breach, unexpected orientation failure,
  resolver setup error, or metadata-hash mismatch. A capped
  `capped_pilot_diagnostic` result is reported as diagnostic, never as a
  whole-source qualification.
- A full 6,022 run is a separate parent decision with a fresh measured budget.

## 11. Identity ordering and migration seam (unchanged, not activated)

Provisional draft only: observed OGS-00012 EUR / 00013 AFR / 00014 AMR /
00015 EAS, META observed-only OGS-00017, completed definitions
OGS-00018/19/20/21; FinnGen stays **OGS-00016**. Placing META ahead of FinnGen
would reuse `OGS-00016`, colliding with the existing artifact path and
reassigning an identifier ADR 0022 says is never reassigned; that needs an
explicit ADR 0022 amendment and an old→new migration note. **No renumbering is
performed or requested here.**

## 12. Handoff

This plan is code/documentation only; it opens no association root, runs no
resolver, and touches no genotype. Parent and owner/operator review are
requested; the first actionable step after the §6 read-scope authorization is
the metadata-only selection TSV, then a bounded resolver pilot with the measured
receipt.
