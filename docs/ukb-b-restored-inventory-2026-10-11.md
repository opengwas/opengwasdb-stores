# Restored full UKB (ukb-b) Source Inventory and candidate — acceptance report (#219)

Snapshot `ukb-b-restored-2026-10-11`. Branch `ticket/219-restored-ukb`. This
report is Phase B evidence only: it freezes and reconciles the restored input
set and emits a candidate Release Bundle for explicit human acceptance. It does
not build, complete, accept, validate or publish a Store.

## Why this exists

The registered `OGS-00009` observed Store and its `OGS-00010` descendant contain
2,024 Analyses because ~490 declared ukb-b Analyses had not finished copying
into `/data/opengwasdb/raw/gwas-vcf/ukb-b`. The ticket requires the restored set
to be reconciled against the declared intention, frozen with file identity, and
compared with the incomplete baseline — without assuming the final total.

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

## Membership differences against the old built baseline

- The old 2,024-Analysis set is a strict subset of the 2,514 intended Analyses.
- Of the 2,514 intended, **2,511 are buildable** and **3 are meaningful
  exclusions** carried from the recorded resolution: `ukb-b-12026`,
  `ukb-b-1226`, `ukb-b-1585` ("no gwasinfo record returned by the OpenGWAS API").
- `ukb-b-1585` **is** present in the old 2,024-Analysis build. The restored
  candidate excludes it as unresolved, so the restored observed Store would not
  be a strict superset of the old built set (2,023 of the old rows build, plus
  488 newly restored rows). This is a decision for the human acceptance gate:
  either accept the 2,511 buildable membership with the three audit exclusions,
  or resolve the three upstream records (needs a working `OPENGWAS_JWT`) before
  the build. It must not be resolved by silently defaulting metadata.

## Frozen-input receipt

| Artifact | sha256 |
|---|---|
| Source Inventory `resources/inventories/ukb-b-restored-2026-10-11.tsv` (2,514 rows, 425,402,488,477 bytes ready) | `3784abade55150684fb4c837ccb49195d09c50540b940356e48f5887fa8c14b4` |
| Provenance `…2026-10-11.meta.yaml` | `ad4500d3c7004c16e4e24fc61daccae957cdcc0eb11a109d38e197300c25010f` |
| Upstream batch evidence `…2026-10-11.upstream-batches.json` (`ukb-b` count 2,514) | `f108faee01dbaffe9bd2114cdfbb8e423d852b30ff78ca4e30904d211e641457` |
| Declared selection `families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv` | `0ae5c2d74d46dfcea7b2fcb36f3032e1ca4ae27117a1d6ac6915400cde060284` |
| Recorded resolution `families/ukb-b/releases/dense-observed-vcf-c128-resolved/analyses.tsv` | `8c5de792dbf8eafb975e75d834b8a67d9aa9034963517a9bf0a1d0d40dcbb5e5` |
| Old baseline `stores/OGS-00009/analyses.tsv` | `eacfac7d98bf441bd9b35b1ba0779a4cbee26799cb7166c7022e1bf44ea6866a` |
| Resumable checksum receipt `/data/opengwasdb/work/epic232/219/ukb-b-restored-2026-10-11.checksums.json` (2,514 entries) | `0bf039ffc2adda4cf45880d2bfdc32ee643d9188338e0842bd9bd8674c9e52b5` |
| Committed receipt `resources/inventories/ukb-b-restored-2026-10-11.receipt.json` | see the file |

Each inventory row records the exact `source_file`, `data_bytes`, `sha256`,
`mtime_ns` and `index_file`. The freeze read 410 GB with two reader threads in
258.9 s and refuses to freeze anything but the exact intent, so a partial copy
can never be recorded as the frozen input set. A **deep re-verification**
(`inventory.py verify --deep`, two readers, `/data/opengwasdb/work/epic232/219/verify-deep.json`)
re-read and re-checksummed all 2,514 files against the frozen snapshot: 0 missing,
0 size mismatch, 0 mtime change, 0 checksum mismatch. `…receipt.json` binds these
hashes into one committed artifact.

## Candidate Release Bundle

Proposed identity/lineage (awaiting orchestrator confirmation before it is
published into `stores/`):

- **`OGS-00017`** — restored full ukb-b observed (`dense`, `observed_only`,
  `status: candidate`), membership = the frozen 2,514-row inventory, 2,511
  buildable, 3 preserved exclusions.
- **`OGS-00018`** — its Reference-Completed descendant, `derived_from:
  OGS-00017`, EUR hGDP+1kGP hg38 LD panel (built by #222).
- `OGS-00009` / `OGS-00010` are left untouched as the historical incomplete
  baseline; `OGS-00008` (pilot) is likewise untouched.

The candidate is emitted by
`resources/generators/ukb-b/generate_candidate.py` and only published after
`bundle.check()` and the shared OpenGWASDB `analyses.tsv` schema pass. It takes
every source path, size and checksum from the frozen inventory — never from the
recorded resolution table, which still names another host's `/local-scratch`
paths — preserves the three exclusions with their reasons, and refuses to
overwrite an existing bundle.

## Limitations and blockers (not silent substitutions)

1. **The stored `OPENGWAS_JWT` expired 2026-08-21.** `POST /api/gwasinfo`
   returns HTTP 401, so live re-resolution of effect-scale/sample-size metadata
   is unavailable. The candidate therefore consumes the **recorded** resolver
   output `dense-observed-vcf-c128-resolved/analyses.tsv` (snapshot
   `opengwas-api-r2026-08-10`), and `release.yaml` records that provenance. The
   unsecured `/api/batches` endpoint still works and is what anchors the intent
   count.
2. **Ancestry is a source fallback, not an AF assignment.** The ukb-b VCFs carry
   a `FORMAT/AF` field (verified in the `ukb-b-10001` header) and the recorded
   pilot is `af_assigned` EUR, so the parent's legacy
   `ancestry_assignment_method: source_trusted_no_af` is factually wrong here.
   The restored candidate uses `source_fallback` (source label `European`,
   `assigned_ancestry: EUR`) rather than fabricating `af_assigned`, and
   `validation.yaml` records `checks.ancestry: not_run` with a warning.
   Running `opengwasdb resolve-analyses` (the one-pass, bounded resolution
   interface) to produce genuinely `af_assigned` ancestry and SD evidence is a
   #222 step; this ticket did not run it because its granted resource envelope
   is read-only source-identity hashing.
3. **Runtime adoption is #218's.** The `hm3` Indexed Variant Subset in the
   recipe mirrors `OGS-00009`; the format-0.2.0 runtime that provides
   `build-indexed-subset` is adopted by #218 before #222 builds.

## Checks run

- `tests/ukb-b/test_ukb_inventory.py` — 14 tests, pass.
- `tests/ukb-b/test_ukb_generate_candidate.py` — 9 tests, pass.
- `python3 resources/scripts/check_bundles.py` — 12/12 registered bundles pass.
- `python3 resources/scripts/run_all_tests.py --only python` — 40/43 suites
  pass. `qc-panel-concordance`, `workflow` and `release-faults` fail identically
  at the base commit `3a9cf78` (verified in a clean worktree); they are
  pre-existing environment failures from the shared main-checkout `opengwasdb`
  (which now needs `zarr>=3`'s `BloscCodec` while the shared Pixi envs still
  have `zarr 2.18.7`), not regressions from this change.

## Deliberately excluded

No production Store build, Reference Completion, publication, acceptance,
artifact deletion, identity renumbering or edit to `OGS-00009`/`OGS-00010`.
Live API re-resolution and AF-based ancestry assignment are reported as
blockers, not worked around. Publishing the candidate into `stores/` waits for
explicit release-gate confirmation of the proposed ids.
