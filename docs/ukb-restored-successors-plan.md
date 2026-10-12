# Restored UKB successors: identity, completion-panel and qualification plan (#219)

Owner decisions cited:
<https://github.com/opengwas/opengwasdb-stores/issues/219#issuecomment-6132878514>
(the 2,514 replacement + production UKB EUR panel path) and
<https://github.com/opengwas/opengwasdb-stores/issues/219#issuecomment-6133853820>
(successor identity / replacement lineage / bundle-retention clarification).
The restored 2,514-Analysis target is accepted as the replacement for the
incomplete `OGS-00009`/`OGS-00010` pair; the successor identities are
**`OGS-00030`** (observed) and **`OGS-00031`** (completed). Historical **Store
artifacts must be preserved**; retention of the old **Release Bundles** is
waived only in the sense that their definitions need not be permanently
archived — they are retired, not deleted, **once successor activation is
genuinely ready**. Nothing is deleted now: `OGS-00030` is only staged and
`OGS-00031` is absent, so the active bundles stay, and minimal artifact lineage
/ evidence / Git history is retained. This records identity, lineage and a
qualification plan only: **no acceptance, no production build, no `OGS-00031`
construction, no `OGS-00030` build.**

Branch base: `dev@1db347d922a53aa6e057da2a08575aa53259918a` (clean feature branch
`ticket/219-ukb-successors`); the previous reviewed checkpoint `d4192a6` is
preserved on `ticket/219-restored-ukb`.

## Identity and replacement lineage

Machine-readable codification: `resources/generators/ukb-b/successor-identity.yaml`.
Lineage source (owner, durable): issue comment 6133853820.

| Historical (preserved) | Successor | Relation |
|---|---|---|
| `OGS-00009` observed, 2,024 | `OGS-00030` observed, 2,514 | replacement observed release |
| `OGS-00010` completed, 2,024 | `OGS-00031` completed, 2,514 | replacement completed release, `derived_from: OGS-00030` |

The Release Bundle schema has no `supersedes` field; replacement is recorded in
the codification file and, at the release gate, by the historical releases' own
Status transition. `OGS-00009`/`OGS-00010` are **not edited here**.

## Completion-panel reference: stale declaration and correction plan

The owner-chosen panel is the production UKB EUR LD panel present at
`/data/opengwasdb/reference/ld/ukb`. The committed resource
`resources/reference-resources/gpm-hg38-eur-ld/resource.yaml` still points at
the absent external path
`/local-scratch/projects/genotype-phenotype-map/data/ld_reference_panel_hg38`
with `ancestry_dir: EUR`; `ukb-hg38-eur-af` points at the absent
`/data/opengwasdb/reference/ukb-hg38`. Proposed corrected declaration (inert,
for review): `resources/generators/ukb-b/proposed-ld-resource.yaml`.

**Path/flag constraint.** `opengwasdb complete-dense --ld-panel <root>
--ancestry <label>` uses the single `--ancestry` string as **both** the panel
directory segment (`<root>/<label>/<chr>/…`) and the ancestry-match filter
(`derive_impute_analysis_ids`, which raises `AncestryFilterError` when nothing
matches). With the directory named `ukb` and the Analyses' `assigned_ancestry`
`EUR`:

| Invocation | Result |
|---|---|
| `--ld-panel /data/opengwasdb/reference/ld --ancestry ukb` | chromosomes found, but `AncestryFilterError` (`ukb` normalises to no super-population; rows are `EUR`) |
| `--ld-panel /data/opengwasdb/reference/ld --ancestry EUR` | filter matches `EUR`, but `LdPanelNotFoundError` (no `.../ld/EUR`) |
| `--ld-panel /data/opengwasdb/work/epic232/219/reference-root --ancestry EUR` | **works** — the isolated, non-destructive `reference-root/EUR -> /data/opengwasdb/reference/ld/ukb` symlink satisfies both the path and the filter |

**Plan.** The production fix is a minimal, non-destructive `EUR -> ukb` alias at
the declared reference root (owner/infra), after which
`--ld-panel /data/opengwasdb/reference/ld --ancestry EUR` resolves both axes with
no change to `assigned_ancestry` or to core. The isolated work root above provides
the same resolution for qualification today without mutating the physical panel,
old Stores, mirror or production registry. To be precise about "pending": the
isolated `reference-root/EUR` symlink **already exists** (created for
qualification); what remains pending is the **production alias/declaration
activation** and the resource-declaration review, not the link creation itself.

Exact future completion flags (once the alias and the release gate exist):

```sh
opengwasdb complete-dense <OGS-00030 store.opengwasdb> <OGS-00031 store.opengwasdb> \
  --ld-panel /data/opengwasdb/reference/ld \
  --ancestry EUR \
  --release-id OGS-00031 \
  --min-cor 0.7 --thresh 0.9 \
  --n-workers <N>
```

(no `--overwrite`; `--n-workers` and resource envelope are a separate release
decision).

## Read-only qualification (<=1 panel reader, metadata and one representative block)

- **Layout.** 22 chromosome directories (1–22); **1,357** block `.tsv` pairs with
  **1,357** `.ldeig.npz` and **1,357** `.afreq`; optional `.unphased.vcor1.gz`
  present; `n_blocks` matches `gpm-hg38-eur-ld`.
- **Representative block** `1/101384499-103762931`: TSV header `CHR SNP OA EA EAF BP`,
  9,633 variants; core `load_block()` reports `n_ld_snps = 9633` and `eaf` length
  9,633; `.ldeig.npz` holds `values` shape `(9633,)` and `vectors` shape
  `(9633, 250)`, dtype float64. **Dimension agreement only** — this is *not* an
  ordinal, SNP-id or allele-level identity qualification, and the core
  `load_block()` succeeding is a partial dimension-contract success, not a
  biology PASS.
- **Samples/assembly.** The block `.log` shows PLINK on
  `.../ld_reference_panel_hg38/EUR/<chr>/<block>` with **49,766** samples.
  **GRCh38 spot evidence:** 841 of the block's 9,633 canonical ALIDs match the
  maintained HapMap3 GRCh38 ALID list exactly on position **and** allele pair.
  This is *spot evidence toward* GRCh38 corroborating the
  `ld_reference_panel_hg38` source path; it is **not** proof for the whole panel
  and does **not** support a categorical claim that an hg19 panel could never
  match.
- **Key flags.** `--ld-panel` root, `--ancestry EUR`, `--min-cor 0.7`,
  `--thresh 0.9`, `--release-id`, `--n-workers`; core consumes `.ldeig.npz`
  (eigendecomposition), not the co-located `.ldeig.rds`.

## Remaining verification gaps (retained, not waived)

1. No active in-repo Reference Resource declaration for this panel; the
   `proposed-ld-resource.yaml` needs review. The isolated `reference-root/EUR`
   symlink already exists; what is pending is the production alias/declaration
   **activation**, not link creation.
2. Assembly qualification is a bounded spot-check plus source-path evidence, not
   a whole-panel coordinate audit.
3. Per-block integrity (`.npz`/`.tsv`/`.afreq` across all 1,357 blocks) is not
   verified; only one representative block was inspected and no hashing was
   performed. A bounded multi-block integrity sample is a candidate next step.
4. `structural_ld_blocks_lookup.tsv` (610,314 per-SNP lookup rows) was read only
   for its header/count; its relationship to the 1,357 blocks is unqualified.
5. Core **#277** (assign-ancestry liftover) and **#207** (resolve-analyses
   MetricsReader) remain open; candidate ancestry is `source_fallback`.
6. `OGS-00031` is not constructed and `OGS-00030` is not built; identity is
   codified but no release acceptance is implied.

## Non-actions

No mutation of the physical panel, `OGS-00009`/`OGS-00010` Stores, the source
mirror or the production registry. No production alias/declaration activated
(the isolated work-root `reference-root/EUR -> …/ld/ukb` link exists, for
qualification only). No new full-panel reads until parent review and a
qualification budget; at most one panel reader, shared with the single MVP
converter reader. No candidate regeneration, no Store construction, no build, no
heavy LD scan, no ancestry re-run. No bundle deletion: old bundle definitions
may be retired only when successor activation is genuinely ready, active
bundles are not retired while `OGS-00030` is unbuilt and `OGS-00031` is absent,
and minimal artifact lineage / evidence / Git history is retained regardless.
