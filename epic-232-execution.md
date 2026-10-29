# Epic #232 execution record

Scope authority: [epic #232](https://github.com/opengwas/opengwasdb-stores/issues/232),
the revised approved scope of #216. This record describes implementation and
integration evidence, **not** qualification of the full release matrix.

## Current integration checkpoint — 12 October 2026 UTC

The owner authorised review, conflict resolution and PR merges to `dev` after
another orchestration session advanced the work. This supersedes the initial
no-merge instruction; it does **not** accept scientific candidates or authorise
production builds, conversions, completion, validation or publication.

Review baseline: `dev@3f18ccbb75140828691de2574d9f63e6848dda36`.
All four frontier implementation PRs are now merged; stores `dev` is
`5536907e4a094f6fb9b455eb583278076c3ce428` after integration. The primary
checkout's unrelated untracked documentation/model files are preserved.

| Ticket | PR | Checkpoint | Integration state |
|---|---|---|---|
| #217 readiness | [#234](https://github.com/opengwas/opengwasdb-stores/pull/234) | `7336e37` | merged as `7f43c77` after conflict resolution, independent GO and fresh green CI |
| #218 runtime | [#237](https://github.com/opengwas/opengwasdb-stores/pull/237) | `af6fc75` | merged as `3f18ccb`; core pin `4e92256` |
| #219 UKB inventory/candidate tooling | [#236](https://github.com/opengwas/opengwasdb-stores/pull/236) | `40ac43b` (fix `d4192a6`) | merged as `5536907` after correction review and fresh green CI; candidate unaccepted |
| #220 MVP acquisition tooling | [#235](https://github.com/opengwas/opengwasdb-stores/pull/235) | `cbac5b9` | merged as `d0b55e7` after fresh Standards/Spec GO and 27/27 tests; scientific gate HOLD |

#234's only manual conflict was the workflow specification's module list. The
resolution preserves `resources.py`, `runtime.py`, `release.py` and
`readiness.py`; the test-runner changes combine automatically. Local regression
runs use at most four CPUs and BLAS threads one. Fresh CI passed the complete
lightweight suites, bundle checks, index regeneration/clean-tree and docs smoke
on #234 (`7336e37`) and #236 (`40ac43b`); #235's unchanged reviewed head retained
its green CI. Independent Standards and Spec reviews plus targeted integration
checks govern merging, not stale verdicts.

#236 re-review found that `bool(ancestry_records)` could mark ancestry `passed`
even when supplied records were partial or non-AF assignments. The correction
at `d4192a6` requires full admitted AF-assigned coverage, retains `not_run` with
warnings for partial/fallback/unassigned records, rejects out-of-inventory IDs
and records ancestry-file SHA/path/counts. Independent Spec GO; orchestrator
re-ran 46/46 UKB tests. Final integration `40ac43b` includes current runtime and
MVP tooling. No staged candidate or frozen source receipt was regenerated: the
candidate's original generator hash remains accurate historical provenance.
The `release-readiness` Pixi task remains a separate #218 follow-up; the merged
CLI is available directly as `python3 resources/scripts/release_readiness.py`.

#219 and #220 remain open for their outstanding scientific/owner gates; merging
the tooling checkpoints must not auto-close unresolved acquisition or acceptance
work. Unrelated draft PR #207 is outside this epic's merge scope.

## Execution and review gates

1. Reconcile exact intended, manifest, admitted, excluded and actually built
   membership. Missing, duplicate, truncated or mismatched files fail loudly.
2. Explicit human acceptance freezes the Release Manifest Candidate. Material
   membership/recipe choices require a new immutable identity and lineage.
3. Verify the pinned runtime, required capabilities, format-compatible parent
   and ancestry-/assembly-matched Reference Resources before writes.
4. The existing Phase A `pixi run release` interface owns locking, recovery and
   execution. No direct Snakemake bypass or registry-side association processing.
5. Every full release ticket includes its scientific checks, applicable required
   Ragged/Overflow variant indexes and optional Dense-component HM3, exhaustive
   validation, genuine warnings and registration receipts. Fixture tests are not
   full-scale qualification evidence.
6. Code-review GO and code merge are not scientific acceptance or permission to
   replace published Stores. Preserve historical artifacts and valid checkpoints.

## Resource envelope

- Lightweight tests: at most four CPUs per worker, BLAS threads one.
- Bulk source scans: one coordinated slot, at most two readers. #219's completed
  source verification released that slot; no new bulk scan is authorised here.
- #220 may inspect mapping tables and named metadata/header probes; no recursive
  multi-TB content scans or automatic downloads/conversions.
- Full release execution needs its own acceptance gate and resource allocation.
- No recursive delegation; each ticket has a DeepSeek Flash worker and separate
  Gemini 3.8 Flash reviewer in an isolated ticket worktree/Herdr tab. Session
  handles are ephemeral operator state, not release provenance.

## Core #254 and adopted runtime

Core #254 merged through [core PR #276](https://github.com/opengwas/opengwasdb/pull/276)
as `4e922566d7fa4c13a11be3637069da63e48be28c`. The separately owned implementation
passed 2,327 tests. Reported full-run peak RSS dropped 33.7→2.35 GiB (OGS-00009)
and 36.5→3.01 GiB (OGS-00010), both validating clean; wall times fell 88→71 and
135→109 minutes. Its two validation processes finished. These measurements are
not a concurrency budget or qualification of new releases.

Stores #218 initially pinned core `1be2a156649e4d444f31f9b4107c3f47a046f855` and
then repinned to `4e922566d7fa4c13a11be3637069da63e48be28c` at `af6fc75`.
Independent reviewer GO and orchestrator verification confirmed equality of the
runtime constant, `pixi.toml`, `pixi.lock` and installed `direct_url.json`.
Python 3.12, zarr 3.4.1 and numcodecs 0.17 remain unchanged. Format 0.2.0,
fail-closed executable-bound revision checks and scheduled-step-aware completion
gates are preserved. No historical Store was converted or requalified by this
upgrade.

Repin checks: runtime-guard 19/19, plan 91/91, workflow 29/29, bundle-check 12/12,
env-check and lock-check. Read-only real-host smoke refused OGS-00010
complete-only against its 0.1.0 parent; same-run OGS-00009 build + OGS-00010
complete and enhancement-only reruns passed the guard.

## Readiness evidence (#217)

Original exact reviewed checkpoint: `886082bb0f83dd9d9f84cf79972923bb5c377d92`,
independent GO; original CI passed in 16m34s. Orchestrator checks: 26 readiness
tests, 12 index tests, 54 bundle test cases / 128 checks and bundle-check 12/12.
Two explicit legacy #134 tolerated gaps remain visible.

The view separates intended Source Inventory, manifest/admitted/excluded IDs,
actual built IDs, source acquisition, observed build, completion, required
indexes, optional HM3, validation, registration, format/runtime, lineage and
warnings. Built IDs use `read_analyses(open_store(path).analyses_path)`; historical
committed baseline differences are a separate channel. Configured, unknown,
missing or historical evidence cannot become a current clean pass.

OGS-00011's target is exactly 3,169 admitted Analyses; the 3,317-Analysis published
artifact remains historical. OGS-00009/00010 remain the incomplete historical
UKB pair. FinnGen history remains preserved, including the receipt's `'1.0'`
versus published `'0.1.0'` discrepancy on local evidence branch
`fix/ogs-00016-build-tmpdir@c871b61`; no blanket branch adoption occurred.

## Restored UKB evidence and acceptance (#219)

- Exact intended/available/provider-resolved membership: **2,514**, not an
  approximate 2,524; **490 restored** beyond the historical 2,024 built IDs.
- Frozen per-file data and index identities; source bytes 425,402,488,477. SHA
  freeze took 258.9 seconds with two readers. Full decompression and Tabix contig
  checks passed 2,514/2,514; retain their recorded coverage limitations.
- Three historical API-missing records were resolved independently with
  exact-ID/batch-checked, checksum-bound provider JSON. Comparison coverage is
  shared 2,514 / evaluated 2,511 / skipped 3, zero evaluated-field disagreements;
  provider-only resolutions are not API comparison matches.
- Candidate **OGS-00030** remains `candidate`, unaccepted and unregistered, staged
  under `/data/opengwasdb/work/epic232/219/candidates/OGS-00030`.
  **OGS-00031** is not emitted; its completion reference is unresolved.
- Durable evidence lives under `/data/opengwasdb/work/epic232/219/` and committed
  `resources/inventories/ukb-b-*`. No historical OGS-00009/00010 artifact changed.
- Original hermetic checks: 38 UKB tests and bundle-check 12/12; staged candidate
  check had zero errors/tolerated gaps. The ancestry-evidence correction adds
  eight hermetic regressions (46/46 total); its exact-head verdict is separate
  from the original staged emission.

The ten-Analysis trial measured ancestry at ~26.6 minutes and SD at ~1.9 minutes
(two workers, BLAS one). All ancestry outputs were residual-gated `Unassigned`:
GRCh37 source coordinates were queried against a GRCh38 reference without
liftover. This is not valid calibration evidence or a valid lifted full-run
cost estimate. Candidate ancestry remains `source_fallback`, check `not_run`;
SD trial output did not replace candidate metadata. No full 2,514 run authorised.

## Owner and core decision queue

- **UKB acceptance:** explicit acceptance of the staged 2,514 membership and
  material Build Recipe before any full build/registration.
- **UKB completion reference:** historical HGDP+1kGP EUR is calibration-only;
  production GPM UKB EUR is a different panel. Both panels are absent on this
  host; neither reuse nor switching is auto-approved.
- **MVP D1 `GCST90476298`:** Catalog N=235 vs dbGaP `Platelet_Mean_INT.AFR`
  N=114,731. Owner must choose provenance correction/binding or unavailable.
- **MVP D2 `GCST90481129`:** Catalog four-ancestry META N=577,873 vs dbGaP
  three-ancestry META N=571,620; difference exactly EAS 6,253. Owner must decide
  intended Analysis versus distinct/unavailable deposit, or documented correction.
- **57 MVP labels:** 30 punctuation-only and 27 lexical differences require
  recorded policy/individual review; no fuzzy automatic binding. Bijection stays
  red at 6,020/6,022; no conversion or manifest promotion. Acquisition tests 27/27.
- **Core [#277](https://github.com/opengwas/opengwasdb/issues/277):** ancestry CLI
  liftover wiring; [#207](https://github.com/opengwas/opengwasdb/issues/207) owns
  the one-pass resolver's GWAS-VCF rejection. Separate ancestry/SD commands read
  VCF; do not claim all VCF resolution is unsupported. No registry pre-lifting or
  residual-threshold relaxation.
- **AMR completion:** #229 needs an explicitly suitable LD resource; HGDP resource
  declares AFR/EAS/SAS, not AMR. Missing reference is not observed-only success.

## Downstream dependency order

#221 follows #220 and freezes disjoint MVP ancestry/META membership plus identity
planning; FinnGen renumbering, if chosen, must preserve core identity and history.
OGS-00017/00018 remain free; OGS-00030/00031 are UKB candidate proposals only.

#222 follows #218/#219; #223 follows #218/#221. #224 (Chen EUR Ragged) and #225
(Catalog 3,169-Analysis Hybrid) follow #218 and their own scientific/reference
gates. Do not restore the 148 routed-out Catalog Analyses or alter MAF/panel
policy. #226–#230 follow #218/#221: META is observed-only; EUR/AFR/AMR/EAS each
require an appropriate observed/completed pair. #231 rehearses bounded ingestion
only after the required full-release gates actually pass.

## Historical execution checkpoints

Initial stores baseline `3a9cf780b5b77427f275533213b8a2c94b2afc82`; initial core
baseline `1be2a156649e4d444f31f9b4107c3f47a046f855`. Copy completion removed the UKB
copy prerequisite, not identity verification or acceptance. Initial runtime and
UKB candidate NO-GO verdicts were corrected through review rounds; later exact
SHAs supersede them. PR #233 records this chronology, not full epic completion.

Missing required completion, membership acceptance or correctness gates prevent
a blanket mass-ingestion-readiness claim.
