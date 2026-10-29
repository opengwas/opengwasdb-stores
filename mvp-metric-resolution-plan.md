# MVP metric-resolution plan (PMID 39024449) — planning checkpoint

**Status:** plan for parent review. **Not** an operational GO. No resolver run,
no association-body open, no Store build, no scientific acceptance.
**Ticket:** #221 (planning follow-up). **Prepared against** `dev@2075cbf`.
Supersedes `ba0f2e1`; that history is preserved.

## 1. Scope and non-goals

Resolve the pending MVP candidate Analytical Metadata (Assigned Ancestry,
phenotype SD, effect scale, source-AF/SE orientation evidence) through the
**public OpenGWASDB resolver interfaces only**, in a bounded representative
pilot, subject to a new read-scope authorization for the **association bodies
and reference loads** (§6).

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
| Owner decisions | `resources/generators/dbgap-mvp/dbgap-phs002453-key-discrepancies.yaml` (D1/D2/traits; dbGaP authoritative) |

**State is tracked definitions only.** There is **no real emitted candidate
Release Bundle**: `resources/generators/dbgap-mvp/candidates.yaml` is a tracked
plan/definition view, and `generate_candidate.py emit` has never been run. No
`release.yaml:source_snapshot` exists yet; the planned emit will bind the
metadata content hash `1f14…`.

## 3. Public core interfaces (verbatim, no invented flags)

- **`opengwasdb resolve-analyses MANIFEST RECORDS_DIR`** — one-pass resolution to
  `RECORDS_DIR/<analysis_id>.json` + deterministic `index.json`. It has **no
  `--af-source` flag**: the source-MAF versus reference-MAF SD tier is selected
  per Analysis by that row's declared `original_sd_method`.
  Flags for the pilot:
  `--ancestry-reference /data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz`
  (494,105,972 bytes),
  `--ancestry-groups /data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv`
  (385 bytes),
  `--extraction-panel resources/reference-resources/qc-panel-hg38/qc_panel.tsv`
  (420,582 bytes),
  `--default-source-reader-capability opengwasdb.gwas-ssf`,
  `--maf-floor 0.01 --tau 0.5 --delta 0.2 --n-min 5000 --residual-max 0.06 --orientation-flip-r -0.5`,
  `--evidence-sample 20000`, `--max-ancestry-sites 50000` (bounds **ancestry
  evidence only**; a quantitative Analysis still reads to EOF for SD evidence),
  `--max-rows 200000` (exact per-source scan cap for the pilot),
  `--n-workers 2 --resume --largest-first`.
  `--reference-version ukb-ancestry-mixture-hg38`: this is the **actual registry
  resource id**, used as a digest-bound **operator label, not a biological or
  scientific qualification**. The provenance citation is the resource
  declaration `resources/reference-resources/ukb-ancestry-mixture-hg38/resource.yaml`
  (Privé 2022, figshare 31620968, GRCh38 re-key). The receipt records the
  resolver-computed `anc_ref_sha256` from the actual file; this plan does **not**
  re-hash the 494 MB reference.
- **`opengwasdb estimate-phenotype-sd MANIFEST OUT`** — focused SD re-run if
  needed; it does take `--af-source source|reference` (the SD command, not the
  resolver). Not used in the pilot.
- Python API for the registry adapter: `resolve_analyses_manifest(...)` with the
  same arguments; the registry implements no statistics (ADR 0017).

**Fingerprint is inherited, not body-hashed (public read code).** In
`opengwasdb.build.resolve_manifest`, the fingerprint records
`source_recorded_sha256` from the manifest's `checksum`/`sha256` column and
`source_file_size`/`source_file_mtime_ns` from `_stat_source_file` (a `stat()`
call), then `compute_fingerprint_digest`; it does not hash the association body.
`compute_file_sha256` is used only for the ancestry reference, groups,
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

Producing this selection TSV (reading the mapping, manifests and sidecars only)
is **within the existing permitted metadata scope** and does not need the
body-read authorization of §6. It is written with per-row binding evidence, not
summarised labels: `analysis_id`, `mapping_ancestry`, `study_design`,
`mapping_sample_size`, `n_cases`, `n_controls`, `source_file_path` (the
metadata-root symlink), `source_file_size` (target `stat`),
`source_genome_build` (sidecar), `sidecar_gwas_id`, `sidecar_data_file_name`,
`metadata_content_sha256` (`1f14…`). No core call, no body open.

## 5. Metric criteria and null/pending treatment

- **Assigned Ancestry:** core gates only; `unassigned` and `gate_reason`
  recorded verbatim, never coerced.
- **Effect scale is source-provenance, not a study-design default.** A
  quantitative Analysis is **not** automatically `sd`. The registry records the
  public `StoredEffectScale`/`original_effect_scale` value only when the source
  provenance supports a core-supported conversion (for example a raw-unit or
  standardized `beta`+`se` that the core maps to `sd`, or a declared
  `log_or`/`log_hazard`); unknown or ambiguous provenance stays **pending** and
  is never silently defaulted to `sd`.
- **Phenotype SD — per-trait method eligibility.** Quantitative rows are
  eligible for an actual SD tier (`estimated_from_source_maf`,
  `estimated_from_reference_maf`, `estimated_from_beta_distribution`,
  `source_provided`, `declared_standardised`); **case-control rows are
  legitimately `binary_trait` with no SD**. `RecordStatus=success` means the
  row's *eligible* method resolved, not that every row carries an SD.
  `unavailable` stays pending (blank, no default).
- **Source AF/SE and orientation:** `eaf_orientation`/`eaf_orientation_r`
  recorded; an orientation failure is a controlled failure/pending, never a
  loosened threshold. `standard_error` is exactly what the reader reports
  (`#NA`/absent stays absent).
- **Failures:** core `RecordStatus` `success` / `controlled_failure` / `skip`;
  only `success` with the eligible real method tier counts as resolved. A
  `--max-rows`-bounded run is a **capped diagnostic**: its SD evidence is marked
  `source_evidence_scope: capped_pilot_diagnostic`, and success alone does not
  promote a partial estimate to whole-source qualification.

## 6. Read-scope request (blocker for the body reads only)

The existing #220 owner authority covers only the two conversion inputs
(`GCST90476298`, `GCST90481129`) plus metadata, and forbids reads of the other
6,020 association bodies. **Metadata-only selection (§4) is allowed now.** For
the next pilot, **parent start requires a new owner/operator GO** authorizing:

- one bounded read of each of the 15 named association bodies;
- loading the ancestry reference `ref_freqs.hg38.tsv.gz` (494,105,972 bytes),
  `ancestry_groups.tsv` (385 bytes) and the tracked `qc_panel.tsv` (420,582
  bytes);
- no source-body hashing/checksum recomputation (the resolver inherits the
  recorded SHA/stat, §3) and no separate SD re-scan;
- no genotype/LD/panel build, no Store operation.

## 7. META ancestry semantics

The core always infers a dominant super-population mixture per Analysis and has
**no flag to skip inference**. The registry keeps the core record **immutable**
(its inferred evidence stays as provenance in the receipt) and canonicalises the
META bundle's `assigned_ancestry` to **empty** by explicit registry policy. This
is a policy decision, not a promise that the resolver will not assign an
ancestry; nothing rewrites the core record. META remains observed-only with no
completion.

## 8. AMR classifications and no LD production

The AMR LD calibration pilot is **already approved** (ticket #241, MAF 1%); this
plan does not re-ratify it. The resolver may assign AMR or leave it unassigned.
**No AMR LD production** is assumed: the AMR completed definition stays a
pending, blocked plan until a production qualification exists.

## 9. Proposed resource limits, outputs and provenance

The limits below are **proposed; the enforcing wrapper is not implemented in
this plan**. A future wrapper must: impose a whole-pool wall timeout; measure
aggregate **process-tree RSS** (acknowledging shared-page double-accounting);
apply real CPU affinity and BLAS/OpenMP=1; and isolate failed/partial outputs so
a failed worker cannot leave a partial record in the committed set.

- Proposed: ≤ **2 resolver workers**, ≤ **4 CPU**, BLAS/OpenMP **1**, whole-pool
  wall ≤ **1800 s**, aggregate process-tree RSS ≤ **4096 MB**.
- **Write ownership.** The core owns its per-record and `index.json` writes
  (`_atomic_write_json`: a sibling `.tmp_<name>_<uuid>` file, `fsync`, then
  `os.replace`, with `mkdir(parents=True, exist_ok=True)`) — the adapter does
  not claim these use the registry helper. The adapter preflights a fresh root
  (must not exist; no symlinked ancestors) and uses the exclusive
  `resources/generators/lib/safe_write.py` temps only for its **own**
  `selection.tsv` and `resolution-receipt.json`.
- Staging root (fresh; the actual run root gets a runtime-allocated UTC
  timestamp at START, not the historical metadata timestamp):
  `/data/opengwasdb/work/epic232/221/metric-pilot-staging/`.
- Receipt `resolution-receipt.json` records: core commit `4e922566`, metadata
  content hash `1f14…`, exact reference paths plus the resolver-computed
  `anc_ref_sha256`/`anc_grp_sha256`/`panel_sha256`, the gate flags, per-row
  `RecordStatus`/`original_sd_method`/`stored_effect_scale`/
  `source_evidence_scope`, and the measured wall/RSS.

## 10. Acceptance and stop conditions

- **Pilot accepted only if** every row resolves its **eligible** method
  (quantitative: SD/effect tier; case-control: `binary_trait` + `log_or`), or is
  an explicitly classified `controlled_failure`/`skip`; ancestry/SD/orientation
  outcomes are internally consistent; proposed resources stay inside §9; and the
  metadata content hash still equals `1f14…`.
- **Stop and report** on any budget breach, unexpected orientation failure,
  resolver setup error, or metadata-hash mismatch. A capped result is reported
  as a diagnostic, never a whole-source qualification.
- A full 6,022 run is a separate parent decision with a fresh measured budget.

## 11. Identity ordering and migration seam (owner-authorised)

The 2026-10-13 owner decision ("reorder the IDs") fixed the allocation:
observed OGS-00012 EUR / 00013 AFR / 00014 AMR / 00015 EAS, META observed-only
**OGS-00016**, completed definitions OGS-00018/19/20/21, and FinnGen
**OGS-00017**. FinnGen was migrated OGS-00016 -> OGS-00017 (membership, recipe
and association/index artifacts unchanged; directory, `store_id` and a
`migration`/erratum note only) under ADR 0022's owner-authorized-renumbering
amendment; the old identifier stays discoverable through the migration block,
erratum and git history. The published artifact under the artifact root must be
relocated to `<artifact-root>/OGS-00017/` as a separate operational step.

## 12. Handoff

This document is documentation only; it opens no association root, runs no
resolver, and touches no genotype. It must be independently reviewed before any
PR. The first operational step after the §6 owner GO is the metadata-only
selection TSV (§4), then the bounded pilot with the measured receipt (§9).
