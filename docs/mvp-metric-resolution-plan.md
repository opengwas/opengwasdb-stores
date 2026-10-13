# MVP metric-resolution plan (PMID 39024449) — planning checkpoint

**Status:** plan for parent review. **Not** an operational GO. No resolver run,
no association-body open, no Store build, no scientific acceptance.
**Ticket:** #221 (planning follow-up). **Prepared against** `dev@2075cbf`.

## 1. Scope and non-goals

Turn the visible, pending MVP ancestry/META candidate definitions into resolved
Analytical Metadata (Assigned Ancestry, phenotype SD, effect scale, source-AF/SE
orientation evidence) using the **public OpenGWASDB resolver interfaces only**,
in a bounded representative pilot first.

Non-goals: no full 6,022 resolution, no Store build, no acceptance, no ID
activation, no FinnGen renumbering, no LD-panel production. This plan fabricates
no metric; every value comes from a core resolver run or stays explicitly
pending.

## 2. Frozen inputs

| Input | Path / identity |
|---|---|
| Metadata root (frozen) | `/data/opengwasdb/work/epic232/220/metadata-refresh-parent-baf141c-20261013T123913Z` |
| Content hash (verified) | `1f14b88c2c383d8bac97215a8807503ef600fed2d2a019067e9962777438310f` over 12,049 entries |
| dbGaP mapping | `.../conversion-retry-20261012T223021Z/mapping.tsv` (6,022 bound rows) |
| Candidate table | `resources/data/derived/store-candidates-analyses.tsv` (primary checkout) |
| Core runtime | `opengwasdb` 0.3.0 @ `4e922566d7fa4c13a11be3637069da63e48be28c` (verified 218 env) |
| Source assembly | GRCh38 (native; no liftover) |
| Owner decisions | `resources/scripts/dbgap-phs002453-key-discrepancies.yaml` (D1/D2/traits; dbGaP authoritative) |

The candidate `release.yaml:source_snapshot` already binds this metadata via the
recomputed content hash; a resolver run must record the same hash.

## 3. Public core interfaces (no registry-side statistics)

- **`opengwasdb resolve-analyses MANIFEST RECORDS_DIR`** — one-pass per-Analysis
  resolution to `RECORDS_DIR/<analysis_id>.json` + deterministic `index.json`.
  Flags for the pilot:
  `--ancestry-reference /data/opengwasdb/reference/ancestry-mixture/ref_freqs.hg38.tsv.gz`,
  `--ancestry-groups /data/opengwasdb/reference/ancestry-mixture/ancestry_groups.tsv`,
  `--extraction-panel resources/reference-resources/qc-panel-hg38/qc_panel.tsv`,
  `--default-source-reader-capability opengwasdb.gwas-ssf`,
  `--maf-floor 0.01 --tau 0.5 --delta 0.2 --n-min 5000 --residual-max 0.06 --orientation-flip-r -0.5`,
  `--evidence-sample 20000 --max-ancestry-sites 50000`,
  pilot bounds `--max-rows <bound> --n-workers 2 --resume --largest-first --reference-version <tag>`.
  (Optional `--af-reference ANCESTRY=PATH` for the reference-MAF SD tier; not used
  in the pilot, which defaults to `--af-source source`.)
- **`opengwasdb estimate-phenotype-sd MANIFEST OUT`** — focused SD estimation if a
  per-metric re-run is needed (`--af-source source --n-workers 2`).
- Python API for a registry-side adapter: `resolve_analyses_manifest(...)` with
  the same arguments; no statistics are implemented in the registry (ADR 0017).

## 4. Representative pilot selection (metadata only, deterministic)

Selection rule: per ancestry bucket, sort `analysis_id`, take the first two
`quantitative` and the first `case-control` rows (fill with the next sorted row
if a design is absent). No body is opened to choose. 15 rows total:

| Bucket | Quantitative | Case-control |
|---|---|---|
| EUR | GCST90475091, GCST90475094 | GCST90475169 |
| AFR | GCST90475090, GCST90475093 | GCST90475184 |
| AMR | GCST90475089, GCST90475092 | GCST90475183 |
| EAS | GCST90475346, GCST90475350 | GCST90475203 |
| META | GCST90479500, GCST90479501 | GCST90479527 |

The adapter writes a 15-row pilot `analyses.tsv` (canonical columns) whose rows
come from the mapping + the metadata root's sidecars; no metric column is
invented.

## 5. Metric criteria and null/pending treatment

- **Assigned Ancestry:** core gates only (`--tau`/`--delta`/`--n-min`/
  `--residual-max`); `unassigned` and `gate_reason` are recorded, never coerced.
- **Phenotype SD:** `original_sd_method` from the core tiers
  (`estimated_from_source_maf`, `estimated_from_reference_maf`,
  `estimated_from_beta_distribution`, `source_provided`,
  `declared_standardised`, `binary_trait`); `unavailable` stays **pending**
  (blank SD, no default). A binary trait is `binary_trait`, never an SD.
- **Effect scale:** `sd` for quantitative, `log_or` for case-control
  (`binary_trait`), `log_hazard` only if the source declares it; otherwise
  pending.
- **Source AF/SE and orientation:** `eaf_orientation`/`eaf_orientation_r`
  recorded; an orientation gate failure is a controlled failure/pending, not a
  loosened threshold. `standard_error` is taken from the source exactly as the
  GWAS-SSF reader reports it (`#NA`/absent stays absent).
- **Failures:** core `RecordStatus` `success` / `controlled_failure` / `skip`;
  `controlled_failure`/`skip` rows remain pending in the candidate and are not
  built.

## 6. META ancestry semantics

META is cross-ancestry. Its `assigned_ancestry` must remain **empty** (the core
super-population vocabulary has no META); the resolver must not force a single
super-population. META stays **observed-only** with no completion step
regardless of any EUR panel. The mixture provenance (member names, per-source
ancestry fractions) is retained as provenance only.

## 7. AMR classifications and no LD production

The resolver may assign AMR (or leave it unassigned) from AF mixtures. **No AMR
LD production is assumed**: the AMR completion definition remains a pending,
blocked plan until the calibration proposal in
`docs/amr-ld-panel-proposal.md` is accepted and a qualified panel exists.

## 8. Resource budget and measurement

- ≤ **2 resolver workers** (`--n-workers 2`), ≤ **4 CPU**, BLAS/OpenMP threads
  pinned to **1**; pilot only.
- Measure from the pilot: wall time, peak RSS, per-Analysis read bytes/rows, and
  the number of source bodies opened (one bounded pass per Analysis). Record
  actuals; do not infer from core #254.
- `--max-rows` and `--max-ancestry-sites` bound the pilot; a full run needs a
  new measured budget and parent GO.

## 9. Outputs, provenance and input-read bounds

- Outputs: `records/<analysis_id>.json`, `index.json`, and a registry-side merge
  into the candidate `analyses.tsv`; a `resolution-receipt.json` recording the
  core commit, reference versions, gate flags, metadata content hash `1f14…`,
  per-Analysis status, and measured resources.
- Input reads: only the 15 selected `data_file` symlink targets are opened by
  the core reader, one bounded pass each; no association body is opened by this
  planning task. No hashing of bodies; no source `.gz` scanned outside the
  resolver.

## 10. Acceptance and stop conditions

- **Pilot accepted only if:** all 15 records are `success` or an explicitly
  classified `controlled_failure`/`skip`; ancestry/SD/orientation outcomes are
  internally consistent; measured resources stay inside the §8 budget; the
  metadata content hash still equals `1f14…`.
- **Stop and report** if any source body exceeds the read/time budget, any
  orientation gate fails unexpectedly, the resolver reports a setup error, or
  the metadata hash no longer matches.
- A full 6,022 run is a separate parent decision after the pilot review.

## 11. Identity ordering and migration seam (unchanged, not activated)

Provisional draft only: observed OGS-00012 EUR / 00013 AFR / 00014 AMR /
00015 EAS, META observed-only OGS-00017, completed definitions
OGS-00018/19/20/21; FinnGen stays **OGS-00016**. Placing META ahead of FinnGen
would reuse `OGS-00016`, colliding with the existing artifact path and
reassigning an identifier ADR 0022 says is never reassigned; it needs an
explicit ADR 0022 amendment and an old→new migration note. **No renumbering is
performed by this plan.**

## 12. Handoff

Parent review of this plan is requested before any resolver run. On approval the
next checkpoint is the metadata-only pilot `analyses.tsv` plus the bounded
resolver invocation, with the measured budget and receipt; no Store, acceptance
or SCI claim is made at that stage.
