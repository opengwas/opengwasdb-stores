# Epic #232 execution record

Scope authority: [epic #232](https://github.com/opengwas/opengwasdb-stores/issues/232),
the revised approved scope of #216. This is an execution record, not a claim that
the full release matrix is qualified.

## Initial checkpoint — 11 October 2026 UTC

Registry baseline: `dev@3a9cf780b5b77427f275533213b8a2c94b2afc82`.
Core baseline: `dev@1be2a156649e4d444f31f9b4107c3f47a046f855`.
The chosen production runtime remains a #218 deliverable; these baseline
revisions do not imply a new runtime has been adopted or artifacts revalidated.

The operator confirms the additional `ukb-b` VCF copy is complete. This removes
#219's copy prerequisite, **not** its file-identity, complete-membership or human
acceptance gates. Never infer a restored total from the approximate missing count.

Core `opengwasdb#254` has an existing implementer and is excluded from this
orchestrator's implementation ownership. Coordinate its integration and expensive
validation runs; do not install its uncommitted checkout as a release runtime.

## Active frontier

Each ticket has an isolated `ticket/<number>-<slug>` worktree and one Herdr tab,
with a `deepseek/deepseek-flash` worker and a separate
`antigravity/gemini-3.8-flash` reviewer. Reviews first identify acceptance risks,
then assess actual code and evidence at a worker checkpoint.

| Ticket | Worker / reviewer | Deliverable | Initial state |
|---|---|---|---|
| #217 | `worker217` / `review217` | truthful release readiness and historical reconciliation | implementation started |
| #218 | `worker218` / `review218` | immutable current-format runtime pin, lock and compatibility guards | implementation started |
| #219 | `worker219` / `review219` | verified restored UKB inventory and unaccepted candidate | implementation started; copy prerequisite satisfied |
| #220 | `worker220` / `review220` | MVP acquisition and mapping reconciliation | implementation started; full-data scan not authorised |

Herdr session handles and local worktree paths are ephemeral operator state, not
release provenance. Discover them from the live session; the initial layout is
also recorded locally in `/tmp/epic232-herdr.json`.

## Dependency order

- #221 follows #220 and establishes disjoint MVP ancestry/META candidates,
  explicit acceptance and release identity/lineage decisions. Any FinnGen
  renumbering must preserve old identity and evidence through supported APIs;
  moving a directory is not sufficient.
- #222 follows #218 and #219: restored UKB observed/completed successors,
  preserving the incomplete OGS-00009/00010 historical pair.
- #223 follows #218 and #221: full FinnGen observed/completed qualification.
- #224 and #225 follow #218: Chen EUR Ragged and the exactly 3,169-admitted-Analysis
  Catalog Hybrid observed/completed pairs. Catalog's old 3,317-Analysis artifact
  is a baseline; do not restore the 148 routed-out Analyses or change panel/MAF
  policy.
- #226–#230 follow #218 and #221. META is explicitly observed-only. EUR/AFR/AMR/EAS
  each require suitable ancestry- and assembly-matched completion resources;
  missing references are blockers, never silent completion exemptions.
- #231 follows #217 and #222–#230. Rehearse bounded multi-release ingestion using
  the existing interfaces, without removing the human acceptance boundary or
  building another scheduler.

## Execution and review gates

1. Source Inventory identity and exact intended/admitted/excluded membership are
   reconciled. Missing, duplicate, truncated or mismatched files fail loudly.
2. Human acceptance freezes the Release Manifest Candidate. New material
   membership/recipe choices require explicit immutable identity and lineage.
3. The pinned runtime, required capabilities and format-compatible parent are
   verified before writes; matching references are verified before completion.
4. The supported Phase A `pixi run release` interface owns locking, recovery,
   execution and publication. No direct Snakemake bypass or historical deletion.
5. A release ticket includes its own scientific checks, applicable required
   Ragged/Overflow indexes and optional Dense-component HM3, exhaustive
   validation, genuine warnings and registration receipts. Fixtures are not
   full-scale evidence.
6. The independent reviewer reports actionable findings and an explicit
   checkpoint verdict. The orchestrator integrates green ticket commits into
   a human-readable `feat/<name>` branch in the primary checkout and raises PRs
   to `dev` at coherent checkpoints. No automatic PR merge is authorised.

## Initial resource envelope

No production build, conversion, completion or exhaustive validation has been
launched by this orchestration checkpoint. Preserve existing publications,
checkpoints and unrelated working-tree changes.

- Four active implementation workers; no recursive delegation.
- Lightweight tests: at most four CPUs per worker, BLAS threads one.
- One bulk source-identity I/O slot: #219, at most two readers/CPU threads,
  resumable receipts under `/data/opengwasdb/work/epic232/219`.
- #220 may inspect mapping tables, manifests and targeted missing-file metadata;
  full multi-TB rereads and downloads/conversions require a separate allocation.
- Full release execution needs a release-specific gate and resource allocation,
  coordinated with core #254. Query RSS is not a validation concurrency budget.

## UKB verification checkpoint — preliminary

#219 reports **2,514 intended Source Analysis IDs**, reconciled between the
legacy declared selection and core batch manifest, with **2,514 available VCFs**
and **490 additions** relative to the 2,024-Analysis historical baseline. These
are preliminary reconciliation counts, not a completed integrity audit or
accepted membership. Source checksum/integrity verification is allocated the
bulk I/O slot (approximately 410 GB, at most two readers).

Three historical metadata-resolution exclusions still require disposition,
including `ukb-b-1585`, which appears in the old built baseline. Intended,
admitted and accepted membership must remain separate until this is resolved.

The orchestrator reserves **OGS-00030 / OGS-00031** for the restored UKB
observed/completed **candidate proposal**. They are not accepted or registered
release identities. OGS-00017/00018 remain unallocated pending #221's MVP/FinnGen
identity plan; OGS-00009/00010 remain unchanged.

There is a completion-reference declaration conflict to resolve: OGS-00010's
historical recipe specifies HGDP+1kGP/EUR, while that Reference Resource describes
EUR as a calibration control and the separate GPM resource declares the
production UKB EUR panel. Neither historical recipe reuse nor switching panels
is an automatic successor-completion approval. Record suitable reference
identity and its approval/evidence before executing completion.

## Initial review checkpoint

Draft [PR #233](https://github.com/opengwas/opengwasdb-stores/pull/233) targets
`dev` from `feat/232-execution-frontier`. It records this execution setup only;
no worker implementation is integrated or ticket completion claimed. The initial
documentation check was `git diff --check`. Implementation checkpoints will be
reviewed and raised as separate coherent feature PRs.

One declaration-level blocker is already visible: `hgdp1kgp-hg38-ld` declares
AFR/EAS/SAS, not AMR, and #171 excludes ancestries without block definitions.
#229 therefore needs an explicitly suitable AMR completion resource; it cannot
be treated as observed-only success. This inspection is not fresh physical-panel
verification or calibration evidence.

Subsequent checkpoints must record actual commits, reviewed scope, commands,
evidence locations, unresolved owner decisions and PR URLs. Missing completion
or correctness gates prevent a blanket ingestion-readiness claim.
