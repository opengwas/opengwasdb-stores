# Restored full UKB (ukb-b) Source Inventory and candidate — acceptance report (#219)

Snapshot `ukb-b-restored-2026-10-11`. Branch `ticket/219-restored-ukb`. This is
Phase B evidence only: it freezes and reconciles the restored input set and
emits a candidate Release Bundle **to a staging directory** for explicit human
acceptance. No Store is built, completed, accepted, validated or published, and
no candidate is registered in `stores/`.

## Why this exists

The registered `OGS-00009` observed Store and its `OGS-00010` descendant contain
2,024 Analyses because 490 declared ukb-b Analyses had not finished copying into
`/data/opengwasdb/raw/gwas-vcf/ukb-b`. The ticket requires the restored set to be
reconciled against the declared intention, frozen with file identity, compared
with the incomplete baseline, and emitted as a candidate — without assuming the
final total.

## Intended / available / built

| Quantity | Value | How it was derived |
|---|---|---|
| Intended membership | **2,514** | OpenGWAS `GET /api/batches` (unsecured) reports batch `ukb-b` `count=2514`; the registry's declared selection `families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv` has 2,514 rows whose `inclusion_reason` cites the OpenGWAS IGD `data/ukb-b/manifest.tsv`; the core manifest's **committed blob** (`git rev-parse HEAD:data/ukb-b/manifest.tsv` = `git hash-object` of the worktree file, sha256 `fb504fcc34534ebf9e0af75b261afa3f0f7bbc483ce1d11889b5866d91ed9ab7`) has 2,514 rows. All three id sets are identical. |
| Available on disk | **2,514** | `discover_source_collection` found exactly 2,514 `ukb-b-<id>.vcf.gz` files, each with its `.vcf.gz.tbi` index, under `/data/opengwasdb/raw/gwas-vcf/ukb-b`. |
| Old built baseline | **2,024** | Re-read through the **approved core public interface** — `opengwasdb.open_store("/data/opengwasdb/stores/OGS-00009/store.opengwasdb")` + `opengwasdb.model.analyses.read_analyses(store.analyses_path)` on the immutable #218 runtime (commit `1be2a156…`) — into the exact-ID receipt `resources/inventories/ukb-b-built-baseline-ogs00009.receipt.json` (2,024 ids, `analysis_ids_sha256 953ea543…`). The committed bundle `stores/OGS-00009/analyses.tsv` is retained only as the legacy registry record (sha256 `eacfac7d…`), never as the authority for the built set. |
| Restored delta | **490** (80.4 GB) | intended − built. |

The final total is **neither assumed nor 2,524**: the "~500 missing" estimate and
a 2,524 guess are both wrong; the declaration, the independent upstream batch
count and the committed core blob agree on 2,514, and 490 were missing.

Reconciliation is exact: 0 duplicate ids, 0 malformed ids, 0 missing from disk,
0 files outside intent, 0 unexpected filenames, 0 missing indexes, 0
identical-content groups, 0 built-but-not-intended.

## Analytical Metadata resolution (offline provider JSON)

The stored `OPENGWAS_JWT` expired 2026-08-21, so `POST /api/gwasinfo` returns
HTTP 401. Each ukb-b Analysis instead carries a provider JSON sidecar
(`<id>.json`) holding the same study-level fields the API served.
`resources/generators/ukb-b/provider_metadata.py` resolves the frozen inventory
from those sidecars with an **exact-ID check** (`json.id` must equal the frozen
analysis id), a batch check (`ukb-b`), each JSON's sha256 recorded, and the same
contract as the live resolver's `opengwas_api.R` (`unit` →
`stored_effect_scale`; a usable `ncase`/`ncontrol` pair → `case_control`,
otherwise `sample_size` → `total`).

Result: **2,514 / 2,514 resolved, 0 unresolved**, all `batch = ukb-b`, all
`population = European`. The cross-check against the recorded
`opengwas-api-r2026-08-10` resolution is reported honestly as **shared ids
2,514 / evaluated 2,511 / skipped 3** (`ukb-b-12026`, `ukb-b-1226`,
`ukb-b-1585` — recorded exclusions whose compared fields are all blank), with
**0 disagreements on the 2,511 comparable rows**. The three skipped rows are
**provider-only independent resolutions, not crosschecked matches**: the receipt
records per-field coverage as well (`stored_effect_scale`/`sample_size_kind`/
`sample_size` compared 2,511, missing 0; `n_cases`/`n_controls` compared 1,844,
missing 667 because `total`-kind rows have no case/control counts). This
dispositions the three historical exclusions from frozen local metadata rather
than defaulting or dropping them, so the restored membership is a strict
superset of the old build. A `ncase+ncontrol` that disagrees with the provider's
declared total N is surfaced in `resolution_notes`, never silently reconciled;
retention of the provider's total N preserves the total-vs-effective-N
distinction. **These are staged admission values pending human acceptance, not
accepted membership.**

Ancestry is the provider's own `population` label with method
**`source_fallback`** — not an AF-based assignment, and never the parent's legacy
`source_trusted_no_af` (the ukb-b VCFs demonstrably carry `FORMAT/AF`).
`af_assigned` is not claimed and `validation.yaml` keeps `checks.ancestry:
not_run` with the gap visible.

## Frozen-input receipt

| Artifact | sha256 |
|---|---|
| Source Inventory `resources/inventories/ukb-b-restored-2026-10-11.tsv` (2,514 rows; 425,402,488,477 data bytes; 3,711,756,173 index bytes) | `48207ab8da03324e98e62b87767b9b90381ac8457b0c8585b952648c0d7a5a26` |
| Provenance `…2026-10-11.meta.yaml` | `7695eedc2b837f1c08b5bd81fe241f96bae548b4f2e5c182a5c91e65f5cace59` |
| Upstream batch evidence `…2026-10-11.upstream-batches.json` (`ukb-b` count 2,514) | `f108faee01dbaffe9bd2114cdfbb8e423d852b30ff78ca4e30904d211e641457` |
| Declared selection `families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv` | `0ae5c2d74d46dfcea7b2fcb36f3032e1ca4ae27117a1d6ac6915400cde060284` |
| Recorded resolution `families/ukb-b/releases/dense-observed-vcf-c128-resolved/analyses.tsv` | `8c5de792dbf8eafb975e75d834b8a67d9aa9034963517a9bf0a1d0d40dcbb5e5` |
| Old baseline `stores/OGS-00009/analyses.tsv` (repo record, not the built-set authority) | `eacfac7d98bf441bd9b35b1ba0779a4cbee26799cb7166c7022e1bf44ea6866a` |
| Provider resolution `resources/inventories/ukb-b-provider-resolution-2026-10-11.tsv` | `62e04f578afaa90f45a44a1042d168986482099b43e65f7a19ad4a85024c9e18` |
| Provider resolution receipt | `df97573038149eb978e3b737758505c4b4ed5c9d815a01c977844f08f020dd9b` |
| Built-baseline exact-ID receipt `resources/inventories/ukb-b-built-baseline-ogs00009.receipt.json` | `c19725b619feb031132d937e0ac3f236cb782d6b270c526b4f291bf85cb5da1b` |
| Resumable checksum receipt `/data/opengwasdb/work/epic232/219/ukb-b-restored-2026-10-11.checksums.json` (2,514 entries) | `0bf039ffc2adda4cf45880d2bfdc32ee643d9188338e0842bd9bd8674c9e52b5` |

Each inventory row records the exact `source_file`, `data_bytes`, `sha256`,
`mtime_ns`, and also the **index identity** `index_file`, `index_bytes`,
`index_sha256`. The freeze read 410 GB with two reader threads and refuses to
freeze anything but the exact intent, so a partial copy can never be recorded as
the frozen input set.

Verification is two separate things and they are reported as such:

- **Cryptographic identity (sha256).** A deep re-verification
  (`inventory.py verify --deep`, two readers) re-read and re-checksummed all
  2,514 data files *and* their indexes against the frozen snapshot: 0 missing,
  0 size mismatch, 0 mtime change, 0 data checksum mismatch, **0 index
  mismatch**. SHA proves the bytes are the frozen bytes; it does not by itself
  prove a valid compressed stream.
- **Compressed-stream integrity (BGZF/Tabix).** `inventory.py integrity` runs a
  full decompression of every `.vcf.gz` (`bgzip -t`, pysam fallback) and opens
  every Tabix index listing its contigs (`tabix -l`), at two readers. Result:
  **2,514/2,514 pass, 0 failures**
  (`/data/opengwasdb/work/epic232/219/integrity-report.json`). A `.tbi` presence
  or a file size is not treated as index verification; the index is opened and
  its contigs listed.

`resources/inventories/ukb-b-restored-2026-10-11.receipt.json` binds these
hashes into one committed artifact.

## Candidate Release Bundle — staged, not registered

The orchestrator reserved the administrative candidate identities **`OGS-00030`**
(observed) and **`OGS-00031`** (Reference-Completed); `OGS-00017`/`OGS-00018`
remain unallocated for MVP/FinnGen. Per the release gate, the candidate is
**not published into `stores/`**: it is emitted to
`/data/opengwasdb/work/epic232/219/candidates/OGS-00030/` and validated there.

- `OGS-00030` — restored full ukb-b observed (`dense`, `observed_only`,
  `status: candidate`), membership = the frozen 2,514-row inventory, 2,514
  buildable, 0 exclusions in the provider-resolution stage. It passes
  `bundle.check()` with no errors and no tolerated gaps and the shared
  OpenGWASDB `analyses.tsv` schema, and refuses to overwrite an existing bundle.
- `OGS-00031` is **not emitted**. The completion reference is an unresolved
  owner decision: `OGS-00010`'s historical recipe names the HGDP+1kGP/EUR panel,
  which the resource declaration presents as a EUR calibration control, while
  `gpm-hg38-eur-ld` declares the production UKB-49,766 EUR LD panel. Neither
  historical reuse nor a panel switch is auto-approved, so the completed
  descendant stays explicitly blocked with a proposed, unreviewed reference.
- `OGS-00009` / `OGS-00010` remain untouched historical incomplete baselines.

The candidate takes every source path, size and sha256 from the frozen
inventory — never from a recorded resolution table, which still names another
host's `/local-scratch` paths.

## Limitations and blockers (not silent substitutions)

1. **Live API resolution is unavailable** (expired token). The offline provider
   JSON fallback is exact-ID checked, sha256-bound and cross-checked against the
   recorded API resolution, which is why it is a disposition rather than a
   substitution. Any later live re-resolution must reconcile against it.
2. **One-pass `resolve-analyses` cannot stream GWAS-VCF — but that is not the
   whole story.** Two independent ten-Analysis runs returned **10/10
   `controlled_failure`** with `TypeError: GwasVcfReader cannot stream source
   metrics; the one-pass resolver reads tabular sources only (issue #207)` and
   `rows_read = 0`: the first on the default shared env's `7b0c42d` build, the
   second on the verified immutable #218 runtime (`1be2a156…`). The code branch
   (`opengwasdb/build/resolve.py` requires a `MetricsReader`; `GwasVcfReader` is
   not one) explains it structurally, so it is not runtime-version-specific.
   The focused core follow-up is **#278** (one-pass resolver cannot stream
   GWAS-VCF metrics); **#207** is the historical SSF-foundation issue (merged via
   #210), not current implementation scope, and **#277** (assign-ancestry
   liftover) remains current.
   **This must not be read as "all AF-based ancestry/SD is impossible".** The
   committed core exposes separate commands that DO read GWAS-VCF — but the
   ancestry command as invoked is assembly-incompatible for ukb-b (GRCh37 source
   vs GRCh38 reference, no liftover); see the measured section below. Evidence:
   `/data/opengwasdb/work/epic232/219/resolution/probe-report.json`.
3. **The declared EUR reference-AF resource is absent on this host.**
   `ukb-hg38-eur-af` (`/data/opengwasdb/reference/ukb-hg38`) and
   `gpm-hg38-eur-ld` do not exist here, so the reference-MAF SD tier and the
   production completion panel are unavailable. Reported, not substituted.
4. **Runtime adoption is #218's.** The `hm3` Indexed Variant Subset in the
   staged recipe mirrors `OGS-00009`; the format-0.2.0 runtime is adopted by
   #218 before #222 builds.

## AF-based ancestry and SD via the separate supported core commands (measured)

The committed core provides `assign-ancestry` (default reader
`opengwasdb.gwas-vcf`, targeted AF extraction at reference sites) and
`estimate-phenotype-sd` (reads a canonical `analyses.tsv` through a
`SourceReader`, source AF by default), both covered by committed tests. A
bounded ten-Analysis trial through the immutable #218 runtime measured:

| Command | Result | Cost (10 Analyses, 2 workers, BLAS 1) |
|---|---|---|
| `assign-ancestry` | Read all 10 (`af_overlap` 29,825–45,484 of 5,810,530 reference sites; `eaf_orientation = passed`, r 0.83–0.88; dominant `EUR` 0.876–0.928) but every one gated `Unassigned` with `gate_reason = residual`. **Not interpretable — see assembly mismatch below.** | 26 m 34 s wall (< 30 min), ~50 m CPU, 8.2 GB peak RSS, ~1.4 GB read |
| `estimate-phenotype-sd` (`--af-source source`, method `estimated_from_source_maf`) | Produced `original_sd` per Analysis (0.078–0.978, dispersion 0.0001–0.004) | 1 m 52 s wall (< 30 min), 1.2 GB peak RSS, ~25 MB read (bounded evidence sample, not EOF) |

**Pilot result, preserved verbatim.** As run, the pilot is **10/10 `Unassigned`
with `gate_reason=residual` under the unchanged `--residual-max 0.06`**. That is
a **failed pilot outcome**, recorded as such: it is **not** a clean gate pass and
**not** evidence that the threshold is wrong, so the threshold is untouched. A
structural assembly gap (below) means the NNLS values are not interpretable as
calibration evidence either. **The full 2,514-Analysis ancestry run was NOT run
and is not authorised** (projected ≈57 h ancestry + ≈8 h SD). No
`source_fallback` → `af_assigned` admission, no beta/SD override and no gate
change is made; any such owner decision needs explicit approval. `OGS-00031`'s
completion reference remains blocked.

**Assembly / liftover convention — the decisive check.** The ukb-b VCFs are
**GRCh37/hg19** (`##contig=<ID=1,…,assembly=GRCh37>`), the ancestry reference is
**GRCh38/hg38** (`ref_freqs.hg38.tsv.gz`, 5,810,530 sites). The `assign-ancestry`
CLI exposes **no `--liftover`/`--chain` option**, `annotate_catalogue` calls
`assign_from_source(...)` with no liftover, and `GwasVcfReader.liftover` defaults
to `None`, so `extract_at_sites` built a `-R` regions file from the **hg38**
reference ALIDs and queried the **hg19** VCF at those coordinates. Source and
reference coordinates were therefore never aligned, and the ~0.5–0.8 % overlap
(29.8k–45.5k / 5.81 M) is consistent with coincidental allele matches. The
`gate_reason=residual` and `eaf_orientation=passed` values are **artifacts of the
assembly mismatch, not a gate/calibration finding**, and are not used to change
the candidate. The core library does document the correct path —
`assign_from_source(..., liftover=load_liftover(chain_file=…))`, the chain file
is present at `/data/opengwasdb/reference/liftover/hg19ToHg38.over.chain.gz`, and
when set the reader does a full unrestricted scan lifting each row — but the CLI
does not wire it. So the **existing route is VCF-capable in principle yet
assembly-incompatible as invoked**, and the fix is liftover wiring (a small core
CLI change) or a registry-side pre-lift, **not** loosening a threshold. The SD
estimate is unaffected: `--af-source source` is assembly-independent and used a
bounded evidence sample. Linear projection at 2 workers for the full 2,514:
**≈57 h `assign-ancestry` + ≈8 h `estimate-phenotype-sd`** (and a liftover run
would be a full scan, so likely more for ancestry) — a material #222 budget item.
Ancestry in the staged candidate stays `source_fallback`; no AF assignment was
admitted. Evidence: `resources/inventories/ukb-b-ancestry-sd-trial-2026-10-11.json`
(with manifest/reference/chain sha256 identities) and the work-dir copy at
`/data/opengwasdb/work/epic232/219/resolution/ancestry-sd-trial.json`.

## Checks run

- `tests/ukb-b/test_ukb_inventory.py` — 19 tests, pass (includes
  changed-during-hash refusal, receipt size/mtime invalidation, index tamper,
  BGZF/Tabix ok/corrupt/missing).
- `tests/ukb-b/test_ukb_generate_candidate.py` — 9 tests, pass.
- `tests/ukb-b/test_ukb_provider_metadata.py` — 10 tests, pass (includes
  swapped/mismatched sidecar id, contradictory case/control totals, a skipped
  row not counting as a verified match, retry determinism).
- `python3 resources/scripts/check_bundles.py` — 12/12 registered bundles pass
  (the staged candidate is not registered).
- Deep identity re-verification and BGZF/Tabix integrity: 2,514/2,514 clean.
- Core-interface baseline read (2,024 ids via `open_store` + `read_analyses`)
  and exact-ID receipt; core manifest verified against the committed git blob.
- Ten-Analysis pilots: `resolve-analyses` 10/10 `controlled_failure` on both
  runtimes; `assign-ancestry` read all 10 but gated all 10 `Unassigned`
  (`gate_reason=residual`) — **invalidated by the GRCh37-source / GRCh38-reference
  liftover gap**, not a calibration result; `estimate-phenotype-sd` produced 10
  SD estimates. Measured costs and projections in
  `/data/opengwasdb/work/epic232/219/resolution/ancestry-sd-trial.json`.
- `python3 resources/scripts/run_all_tests.py --only python` — 41/44 suites
  pass. `qc-panel-concordance`, `workflow` and `release-faults` fail identically
  at the base commit `3a9cf78` (verified in a clean worktree); they are
  pre-existing environment failures from the shared main-checkout `opengwasdb`
  (which needs `zarr>=3`'s `BloscCodec` while the shared Pixi envs ship
  `zarr 2.18.7`), not regressions from this change.

## Deliberately excluded

No production Store build, Reference Completion, publication, acceptance,
artifact deletion, identity renumbering, or edit to `OGS-00009`/`OGS-00010`. The
candidate is not registered in `stores/` and awaits the release gate plus
explicit human acceptance; the completed descendant `OGS-00031` is planned but
not emitted. Live API re-resolution and AF-based ancestry are reported as
blockers, not worked around.
