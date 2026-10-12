# Restored full UKB (ukb-b) Source Inventory and candidate — acceptance report (#219)

Snapshot `ukb-b-restored-2026-10-11`. Branch `ticket/219-restored-ukb`. This
report is Phase B evidence only: it freezes and reconciles the restored input
set and emits a candidate Release Bundle for explicit human acceptance. It does
not build, complete, accept, validate or publish a Store.

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
| Intended membership | **2,514** | OpenGWAS `GET /api/batches` (unsecured) reports batch `ukb-b` `count=2514`; the registry's declared selection `families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv` has 2,514 rows whose `inclusion_reason` cites the OpenGWAS IGD `data/ukb-b/manifest.tsv`; the core `/home/gh13047/repo/opengwasdb/data/ukb-b/manifest.tsv` also has 2,514 rows. All three id sets are identical. |
| Available on disk | **2,514** | `discover_source_collection` found exactly 2,514 `ukb-b-<id>.vcf.gz` files, each with its `.vcf.gz.tbi` index, under `/data/opengwasdb/raw/gwas-vcf/ukb-b`. |
| Old built baseline | **2,024** | `stores/OGS-00009/analyses.tsv` and `stores/OGS-00010/analyses.tsv`, identical sets. |
| Restored delta | **490** (80.4 GB) | intended − built. |

The final total is **neither assumed nor 2,524**: the "~500 missing" estimate and
a 2,524 guess are both wrong; the declaration and the independent upstream batch
count agree on 2,514, and 490 were missing.

Reconciliation is exact: 0 duplicate ids in the declaration, 0 malformed ids,
0 missing from disk, 0 files outside intent, 0 unexpected filenames, 0 missing
indexes, 0 identical-content groups, 0 built-but-not-intended.

## Analytical Metadata resolution (offline provider JSON)

The stored `OPENGWAS_JWT` expired 2026-08-21, so `POST /api/gwasinfo` returns
HTTP 401 and the live resolver cannot run. Each ukb-b Analysis instead carries a
provider JSON sidecar (`<id>.json`) holding the same study-level fields the API
served. `resources/generators/ukb-b/provider_metadata.py` resolves the frozen
inventory from those sidecars with:

- an **exact-ID check** (`json.id` must equal the frozen analysis id) and a
  batch check (`ukb-b`), so a glob or a mismatched file can never supply
  metadata;
- each JSON's sha256 recorded, binding the resolution to frozen bytes;
- the same contract as the live resolver's `opengwas_api.R` (`unit` →
  `stored_effect_scale`; a usable `ncase`/`ncontrol` pair → `case_control`,
  otherwise `sample_size` → `total`).

Result: **2,514 / 2,514 resolved, 0 unresolved**, all provider `batch = ukb-b`,
all provider `population = European`. A cross-check against the recorded
`opengwas-api-r2026-08-10` resolution shows **0 disagreements** on all 2,514
shared ids. This disposes of the three historical exclusions
(`ukb-b-12026`, `ukb-b-1226`, `ukb-b-1585`): they are resolved from frozen local
provider metadata, exact-ID checked and consistent with the recorded resolver —
not defaulted and not silently dropped. The restored release is therefore a
strict superset of the old built set (all 2,024 old rows build, plus the 490
restored rows).

Ancestry is the provider's own `population` label mapped to a super-population
code with method **`source_fallback`** — not an AF-based assignment, and never
the parent's legacy `source_trusted_no_af` (the ukb-b VCFs demonstrably carry
`FORMAT/AF`). `af_assigned` is not claimed. The orchestrator allocated ten
representative Analyses through `opengwasdb resolve-analyses` (two workers, BLAS
threads one) to measure traversal semantics and cost before a full run. That
probe ran in 54 s wall / 8.2 GB peak RSS / ~0.6 GB read, but returned
**10/10 `controlled_failure`** with
`TypeError: GwasVcfReader cannot stream source metrics; the one-pass resolver
reads tabular sources only (issue #207)`. **The current Phase B resolver cannot
read GWAS-VCF sources at all**, so AF-based ancestry/SD resolution of ukb-b is
blocked for #222 independent of cost — a core capability gap, not a resource
limit. Retrying the resolver path cannot make it safe.

## Frozen-input receipt

| Artifact | sha256 |
|---|---|
| Source Inventory `resources/inventories/ukb-b-restored-2026-10-11.tsv` (2,514 rows, 425,402,488,477 bytes ready) | `3784abade55150684fb4c837ccb49195d09c50540b940356e48f5887fa8c14b4` |
| Provenance `…2026-10-11.meta.yaml` | `ad4500d3c7004c16e4e24fc61daccae957cdcc0eb11a109d38e197300c25010f` |
| Upstream batch evidence `…2026-10-11.upstream-batches.json` (`ukb-b` count 2,514) | `f108faee01dbaffe9bd2114cdfbb8e423d852b30ff78ca4e30904d211e641457` |
| Declared selection `families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv` | `0ae5c2d74d46dfcea7b2fcb36f3032e1ca4ae27117a1d6ac6915400cde060284` |
| Recorded resolution `families/ukb-b/releases/dense-observed-vcf-c128-resolved/analyses.tsv` | `8c5de792dbf8eafb975e75d834b8a67d9aa9034963517a9bf0a1d0d40dcbb5e5` |
| Old baseline `stores/OGS-00009/analyses.tsv` | `eacfac7d98bf441bd9b35b1ba0779a4cbee26799cb7166c7022e1bf44ea6866a` |
| Provider resolution `resources/inventories/ukb-b-provider-resolution-2026-10-11.tsv` | `62e04f578afaa90f45a44a1042d168986482099b43e65f7a19ad4a85024c9e18` |
| Resumable checksum receipt `/data/opengwasdb/work/epic232/219/ukb-b-restored-2026-10-11.checksums.json` (2,514 entries) | `0bf039ffc2adda4cf45880d2bfdc32ee643d9188338e0842bd9bd8674c9e52b5` |

Each inventory row records the exact `source_file`, `data_bytes`, `sha256`,
`mtime_ns` and `index_file`. The freeze read 410 GB with two reader threads in
258.9 s and refuses to freeze anything but the exact intent, so a partial copy
can never be recorded as the frozen input set. A **deep re-verification**
(`inventory.py verify --deep`, two readers) re-read and re-checksummed all 2,514
files: 0 missing, 0 size mismatch, 0 mtime change, 0 checksum mismatch. A
**BGZF/Tabix integrity scan** (`inventory.py integrity`, two readers) re-read
each source to EOF and opened its Tabix index: **2,514/2,514 pass**, 0 failures
(`/data/opengwasdb/work/epic232/219/integrity-report.json`).
`…receipt.json` binds the inventory hashes into one committed artifact.

## Candidate Release Bundle

Identity/lineage, using the orchestrator's reserved UKB ids (epic #232 execution
record). `OGS-00017`/`OGS-00018` are left unallocated by that reservation.

- **`OGS-00030`** — restored full ukb-b observed (`dense`, `observed_only`,
  `status: candidate`), membership = the frozen 2,514-row inventory, **2,514
  buildable, 0 exclusions**. Emitted into `stores/OGS-00030/`; `bundle.check()`
  passes with no errors and no tolerated gaps, and the shared OpenGWASDB
  `analyses.tsv` schema passes.
- **`OGS-00031`** — the planned Reference-Completed descendant, `derived_from:
  OGS-00030` (emitted by #222). No completion recipe is committed here: the
  orchestrator recorded a Reference Resource conflict (OGS-00010's historical
  HGDP+1kGP/EUR recipe versus the GPM production UKB EUR panel), and neither
  historical reuse nor a panel switch is an automatic completion approval.
- `OGS-00009` / `OGS-00010` remain untouched historical incomplete baselines.

The candidate takes every source path, size and checksum from the frozen
inventory — never from a recorded resolution table, which still names another
host's `/local-scratch` paths — and refuses to overwrite an existing bundle.

## Limitations and blockers (not silent substitutions)

1. **Live API resolution is unavailable** (expired token). The offline provider
   JSON fallback above is exact-ID checked, sha256-bound and cross-checked
   against the recorded API resolution, which is why it is a disposition rather
   than a substitution. Any future live re-resolution must reconcile against it.
2. **Ancestry is `source_fallback`, not `af_assigned`, and the AF path is
   blocked.** The ten-Analysis probe above proves the current Phase B resolver
   cannot stream GWAS-VCF sources at all (`controlled_failure`, core #207), so
   #222 cannot resolve AF ancestry through `resolve-analyses` without a core
   capability change. Provider `population` is consistent (2,514 × European)
   but is not an AF computation.
3. **Runtime adoption is #218's.** The `hm3` Indexed Variant Subset in the
   recipe mirrors `OGS-00009`; the format-0.2.0 runtime that provides
   `build-indexed-subset` is adopted by #218 before #222 builds.

## Checks run

- `tests/ukb-b/test_ukb_inventory.py` — 16 tests, pass.
- `tests/ukb-b/test_ukb_generate_candidate.py` — 9 tests, pass.
- `tests/ukb-b/test_ukb_provider_metadata.py` — 7 tests, pass.
- The deep inventory re-verification and the BGZF/Tabix integrity scan both
  pass over all 2,514 sources (0 failures).
- The ten-Analysis Phase B resolver probe returns 10/10 `controlled_failure`
  (GWAS-VCF not streamable); evidence at
  `/data/opengwasdb/work/epic232/219/resolver-probe/probe-report.json`.
- `python3 resources/scripts/check_bundles.py` — 13/13 registered bundles pass.
- `python3 resources/scripts/run_all_tests.py --only python` — 41/44 suites
  pass. `qc-panel-concordance`, `workflow` and `release-faults` fail identically
  at the base commit `3a9cf78` (verified in a clean worktree); they are
  pre-existing environment failures from the shared main-checkout `opengwasdb`
  (which now needs `zarr>=3`'s `BloscCodec` while the shared Pixi envs still
  have `zarr 2.18.7`), not regressions from this change.

## Deliberately excluded

No production Store build, Reference Completion, publication, acceptance,
artifact deletion, identity renumbering, or edit to `OGS-00009`/`OGS-00010`.
The candidate is `status: candidate` and awaits explicit human acceptance; the
completed descendant `OGS-00031` is planned but not emitted, because its
completion reference is an open owner decision. Live API re-resolution and the
full-scale AF-based ancestry run are reported as blockers, not worked around.
