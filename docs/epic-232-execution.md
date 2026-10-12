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
| #217 | `worker217` / `review217` | truthful release readiness and historical reconciliation | reviewer GO and CI green at `886082b`; [PR #234](https://github.com/opengwas/opengwasdb-stores/pull/234) to `dev`, not merged |
| #218 | `worker218` / `review218` | immutable current-format runtime pin, lock and compatibility guards | pin/lock and full local suite green; runtime-guard checkpoint NO-GO, corrections pending |
| #219 | `worker219` / `review219` | verified restored UKB inventory and unaccepted candidate | checksum freeze complete; candidate/integrity checkpoint review pending |
| #220 | `worker220` / `review220` | MVP acquisition and mapping reconciliation | targeted mapping investigation; numerical discrepancies unresolved; full-data scan not authorised |

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
are preliminary reconciliation counts, not accepted membership. The source
SHA256 freeze subsequently completed: the worker/reviewer report 2,514 inputs,
425.4 GB (decimal), 258.9 seconds and two readers. Durable receipt:
`/data/opengwasdb/work/epic232/219/ukb-b-restored-2026-10-11.checksums.json`.
This cryptographic identity freeze is **not** proof of complete gzip/BGZF or
Tabix integrity; deep integrity verification and its independent review remain
pending. #219 retains the bulk I/O slot, at most two readers.

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

## Reviewed implementation checkpoint: #217

Independent Gemini reviewer verdict **GO**, no blockers, at exact head
`886082bb0f83dd9d9f84cf79972923bb5c377d92`. The orchestrator fast-forwarded the
four reviewed ticket commits into the primary feature branch
`feat/217-release-readiness` and raised
[PR #234](https://github.com/opengwas/opengwasdb-stores/pull/234) to `dev`.
The PR is not automatically merged; #217 remains pending integration.
CI `pixi test + docs smoke test` passed in 16m34s at the same exact head:
[run 38187753289](https://github.com/opengwas/opengwasdb-stores/actions/runs/38187753289/job/114613681988).
The orchestrator verified the terminal check through the GitHub PR API.

Orchestrator checks against the archived exact head: 26 readiness tests,
12 index tests, 54 bundle test cases / 128 checks, bundle-check 12/12 and
`git diff --check`. The two explicit legacy #134 tolerated gaps remain visible.
The worker also ran manifest, register, plan, run, reconcile-build-yaml,
validation-record and indexed-subset suites. The generated report compares
actual built Analysis IDs through public core metadata APIs; it does not
replace those sets with git-baseline counts. Manifest rows, source intention,
admitted membership, configured evidence, historical validation and current
identity-matching evidence are explicitly distinct.

#219's initial `c4a32d6` candidate checkpoint received **NO-GO**: stale candidate
IDs, unresolved metadata exclusions, incomplete index/stream verification and
missing regression cases. The worker is correcting these; no candidate
acceptance or registry/publication write is authorised by that checkpoint.
Source SHA256 freezing remains valid identity evidence, not full integrity proof.

## Runtime and ancestry-resolution checkpoint

The orchestrator independently checked the #218 worktree's installed runtime
metadata: `opengwasdb 0.3.0`, Python 3.12, canonical Git URL and immutable
`direct_url.json` commit `1be2a156649e4d444f31f9b4107c3f47a046f855`.
This is an isolated installed environment, not a merged runtime-adoption PR or
production qualification of that revision. #218's initial runtime-guard review
is **NO-GO** despite its full local suite passing: unknown revisions must fail
closed, runtime version must belong to the resolved executable, and completion
parent checks must use actual scheduled producing steps rather than merely
release IDs. An enhancement-only rerun must not be treated as a compatible
parent rebuild or blocked by an unused completion recipe. The worker is adding
these regressions and corrections before a new independent verdict.

#219 has a conditional allocation for ten representative Analyses through the
existing core Phase B resolver, at most two workers and BLAS threads one, after
source-integrity checks complete. Quantitative SD resolution can traverse to
EOF despite ancestry sample limits: establish actual traversal semantics and
measure cost before authorising the full 2,514-Analysis resolution. Historical
`source_trusted_no_af` must not be inherited where the VCF demonstrably has AF.
No guessed `af_assigned` method or unrestricted source-ancestry substitution is
permitted. Provider JSON metadata fallback needs exact-ID checks, consistent
values, frozen sidecar identity and a traceable resolution record.

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
