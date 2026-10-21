# MVP ancestry and META candidate emitter (#221)

This directory plans the **separate single-ancestry and cross-ancestry META
Release Bundles** for the dbGaP `phs002453` acquisition of PMID 39024449
(Verma et al., VA Million Veteran Program). It is Phase B tooling only.

It never accepts, registers, builds, completes or publishes a Store, and it
never overwrites an existing bundle. It emits each Store as a schema-valid
`status: accepted` bundle with an `accepted_at`, solely so the dev release
workflow may build it (#247); that is workflow buildability, **not** scientific
release acceptance or publication. Human acceptance stays explicitly **pending**
in `validation.yaml`.

**One id, one bundle (ADR 0037, #308).** Reference completion replaces its
observed Store under the same id, so a Store never gets a second, `derived_from`
linked completed id. A reference that is `qualified` and `active` puts both
phases in that one bundle: `completion_state: reference_completed`, no
`derived_from`, and a `build.yaml` carrying the unchanged `build` block plus the
`complete` recipe. A reference that is `pending`, `hold` or `blocked` — every
reference today — leaves the bundle exactly as it was, observed-only and
buildable, with the recipe recorded beside it as
`<observed id>.completion-plan.yaml`. That receipt is not a bundle: completing
the Store needs the reference to qualify and ADR 0037's one permitted amendment
to an accepted bundle (#305), or a re-emit while the candidate is still
unaccepted.

**Terminology.** The 6,022 rows below are the **intended bound** membership from
the accepted dbGaP mapping. The acquisition is **not accepted** and no Source
Inventory is frozen; membership, Analytical Metadata and every reference remain
pending owner acceptance and qualification.

## The partition

The dbGaP mapping marks each Analysis with the ancestry parsed from its member
name; `META` is the cross-ancestry bucket. The legacy candidate table still
groups META under the European `store_key`, so this tool makes the split
explicit and validates it against the intended bound counts:

| Bucket | Intended bound | Candidate store key |
|---|---:|---|
| EUR | 1,669 | `dense__pmid-39024449__European` |
| AFR | 1,368 | `dense__pmid-39024449__African` |
| AMR | 1,053 | `dense__pmid-39024449__Hispanic-or-Latin-American` |
| EAS | 241 | `dense__pmid-39024449__East-Asian` |
| META | 1,691 | `dense__pmid-39024449__Multi-ancestry` |
| **Total** | **6,022** | |

The legacy European bucket is 3,360 = 1,669 EUR + 1,691 META. The buckets are
disjoint, their union is the whole intended membership, and any drift fails
loudly (`partition.validate_partition`).

## Identity allocation — owner-authorised (2026-10-13)

The owner reordered the identifiers ("reorder the IDs"):

| Store | Id |
|---|---|
| EUR | OGS-00012 |
| AFR | OGS-00013 |
| AMR | OGS-00014 |
| EAS | OGS-00015 |
| META (observed-only) | OGS-00016 |

`OGS-00018`-`OGS-00021` were reserved for ADR 0007's two-id reference completion
(one "lineage-linked completed" id per single-ancestry Store). ADR 0037 retires
them: completion is published under the Store's own id, so no Store is allocated
those ids and none is ever reused. They appear below only as this note. The
generator refuses a config whose Store still declares `completed_store_id`,
naming ADR 0037.

FinnGen was migrated **OGS-00016 -> OGS-00017** so META can take OGS-00016 ahead
of it. The FinnGen bundle (`stores/OGS-00017/`) keeps its membership, recipe
and association/index artifacts unchanged; only the identifier, directory and a
`migration`/erratum note changed. See
[`docs/ogs-00016-to-ogs-00017-migration.md`](../../../docs/ogs-00016-to-ogs-00017-migration.md)
and ADR 0022's owner-authorized-renumbering amendment.

The intended membership/recipe definitions are tracked in
[`candidates.yaml`](candidates.yaml) (generated from the config). They are
**not** Release Bundles and are **not** in `stores/`; every entry records
`in_stores: false` and `acceptance: pending`.

## Planned metadata and provenance

`config-candidate.yaml: metadata` records the current metadata-only evidence
root (parent refresh `baf141c`, code sha + metadata content sha256, membership
6,022, `association_opens: 0`, acceptance = technical metadata evidence only,
not scientific/release acceptance) and the actual primary candidate table
`resources/data/derived/store-candidates-analyses.tsv`.

`plan` and `definitions` read the four regular store manifests under that root
and validate the **exact** ID sets (not just row counts) against the mapping
partition: the combined European manifest must equal EUR + META
(1,669 + 1,691 = 3,360), African 1,368, Hispanic-or-Latin-American 1,053,
East-Asian 241; the sets must be disjoint and every row must carry `status: ok`,
a 64-hex `sha256`, a data file and a genome assembly. The configured snapshot is verified against the actual
`parent-run-summary.json` receipt, and **every required receipt field must be
present with its exact type** (code sha, metadata content sha, membership,
promotion false, store_operations false, association_opens int 0,
metadata_idempotent true, source_metadata_unchanged true, all-zero integer exit
codes) — a missing field is a failure, not a skip. The parent's own content-hash
algorithm is reproduced by the shared
[`metadata_content_sha`](../../../src/manifest_generation/metadata_content_sha.py) helper over the frozen
`gwas-ssf/*/GCST*/*` + `manifests/*.tsv` + `refresh-receipt.tsv` tree (symlinks
recorded by target, regular files by sha256+size, data bodies never opened); the
recomputed value must equal the config value and the receipt value before any
emit, and the tracked definitions record it (`computed_content_sha256`) over
12,049 entries. An independent per-file `metadata_fingerprint` is kept as an
extra audit, and `emit --expected-metadata-fingerprint` can pin it; neither
replaces the frozen content-sha binding. Trait/SD resolution is
recorded as `resolution: pending`; no numerical source metric is invented from a
sidecar, and `actual_completion` is `not_claimed`.

All writes go through the shared
[`safe_write`](../../../src/manifest_generation/safe_write.py) helper: the target and its `.partial` temp
must resolve inside the isolated output root, temps use exclusive `open("x")`,
and a symlinked or existing final is refused. The emitter also refuses the
registry `stores/` tree outright.

## Policy invariants

- **References are never promoted by declaration.** `config-candidate.yaml`
  records each reference's `declared_status` separately from `qualification` and
  `activation`. A declaration of `status: available` is not a qualification or a
  science PASS; the EUR path is additionally known stale (#239) and the whole
  panel qualification is pending. AMR has no declared panel; its pilot (#241) is
  approved but not qualified. Only `qualification: qualified` **and**
  `activation: active` makes the bundle declare a completion.
- **A qualified completion carries a real panel.** The `complete` recipe's
  `ld-panel` is the reference's own `ld_panel`, or the `suggested_ld_panel` path
  the config already declares for it. A qualified, active reference with neither
  fails the emit before anything is written, rather than inventing a path.
- **Pending completion definitions are always recorded.** Every non-observed-only
  Store whose reference is not eligible gets `<observed id>.completion-plan.yaml`.
  Its fields mean:
  - `status: pending` and `human_acceptance: pending` — nothing is accepted;
  - `blocked`, true whenever the Store is not eligible: no `resource_id`, or a
    `qualification` short of `qualified`, or an `activation` short of `active`.
    It is the same predicate as eligibility itself, never activation alone;
  - `reference` — the reference's own declared status, qualification and
    activation, so the reason for blocking is reviewable;
  - `note` — a "do not execute" instruction.

  There is no separate completed bundle to be refused, because there is no
  second id. `candidates.yaml`'s `completed` definition records the same facts,
  plus `declared`: whether this Store's one bundle declares the completion today.
- **META is observed-only.** Its bundle has no `derived_from`, no `complete`
  block and no completion definition, even though an EUR panel exists.
- **Fail closed on membership.** Every intended member must have exactly one
  resolution row and no extra row may be supplied; otherwise nothing is emitted.
- **Non-empty dataset-source binding.** A resolution row must carry a real
  non-empty `source_file`, `source_genome_build`, `source_label` and
  `source_analysis_id` (plus the Analysis metadata columns). No default source
  build, sample size or SD is invented; a blank required value fails the emit.
  Each row's `assigned_ancestry` must match its bucket, except META, whose value
  must be unambiguously empty or a META/multi-ancestry alias (`Multi-ancestry`
  is normalised to empty in the emitted table) and never a single-ancestry
  label. The metadata sidecar must exist and carry a matching `gwas_id`, a
  `data_file_name` matching the manifest data file, and a `genome_assembly` that
  agrees with the manifest and the source build. `sample_size_kind` follows the
  core contract: `total`/`case_control` require a positive integer matching the
  approved manifest N, `effective` requires a finite positive value with a
  documented `sample_size_method`, and any SD is finite positive.
- **Never emit under the registry `stores/` tree.** A candidate passes
  `bundle.check()`, and the Phase A Snakefile targets every valid bundle under
  `stores/`, so an unbuilt candidate written there would become buildable. Every
  mode refuses this checkout's and the canonical `stores/` root, any
  `--registry-root`, any path component named `stores`, and any symlinked
  ancestor, before any `mkdir` or write.
- **Provenance on every bundle.** `release.yaml: source_snapshot` records the
  metadata root, code sha, metadata content sha256, candidate/mapping paths and
  the resolution table's own sha256 (regular metadata only; no association
  hash), plus the planned manifest and its membership count.
- **No invented Analytical Metadata.** `plan` mode writes only the validated
  partition and a per-Store plan. `emit` mode requires a resolution table and
  refuses to write a bundle without one.

## Usage

```sh
# Tracked definitions from the config (not bundles, not in stores/).
python3 -m manifest_generation.providers.dbgap.mvp.generate_candidate definitions \
  --out-dir resources/generators/dbgap-mvp

# Metadata-only plan: writes partition.tsv and candidate-plan.yaml, no bundles.
python3 -m manifest_generation.providers.dbgap.mvp.generate_candidate plan \
  --mapping /data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/mapping.tsv \
  --out-dir /data/opengwasdb/work/epic232/221

# Fixture/authorised emit only: requires a resolution table, writes to staging,
# and runs bundle.check() and Manifest Generation's rules on every emitted bundle.
python3 -m manifest_generation.providers.dbgap.mvp.generate_candidate emit \
  --mapping .../mapping.tsv --resolution /path/to/resolution.tsv \
  --out-dir /data/opengwasdb/work/epic232/221
```

A real metadata emit / full resolution stage requires the parent's go-ahead;
until then `emit` is exercised with fixtures only. Human acceptance, membership
freezing, reference qualification and all execution remain the parent's
responsibility.
