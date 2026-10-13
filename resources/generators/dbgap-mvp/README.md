# MVP ancestry and META candidate emitter (#221)

This directory plans the **separate single-ancestry and cross-ancestry META
Release Bundles** for the dbGaP `phs002453` acquisition of PMID 39024449
(Verma et al., VA Million Veteran Program). It is Phase B tooling only.

It never accepts, registers, builds, completes or publishes a Store, and it
never overwrites an existing bundle. Everything it produces is a
`status: candidate` definition with human acceptance explicitly **pending**.

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

## Identity allocation — provisional draft

`config-candidate.yaml` proposes, but does **not** claim owner approval for:

| Store | Observed | Completed definition |
|---|---|---|
| EUR | OGS-00012 | OGS-00018 |
| AFR | OGS-00013 | OGS-00019 |
| AMR | OGS-00014 | OGS-00020 |
| EAS | OGS-00015 | OGS-00021 |
| META (observed-only) | OGS-00017 | none |

FinnGen is kept at **OGS-00016**. #221 already authorises renumbering FinnGen;
the open item is the ordering choice and the ADR 0022 / public migration of an
existing identity, which is recorded as an unresolved plan exception rather
than treated as a missing permission. The recommended draft avoids the
artifact-path collision that reusing `OGS-00016` for META would create.

The intended membership/recipe definitions are tracked in
[`candidates.yaml`](candidates.yaml) (generated from the config). They are
**not** Release Bundles and are **not** in `stores/`; every entry records
`in_stores: false` and `acceptance: pending`.

## Policy invariants

- **References are never promoted by declaration.** `config-candidate.yaml`
  records each reference's `declared_status` separately from `qualification` and
  `activation`. A declaration of `status: available` is not a qualification or a
  science PASS; the EUR path is additionally known stale (#239) and the whole
  panel qualification is pending. AMR has no declared panel; its pilot (#241) is
  approved but not qualified.
- **Pending completed definitions are always emitted.** Every single-ancestry
  child gets a `<child>.completion-plan.yaml` receipt with `status: pending`,
  `blocked: true` and `human_acceptance: pending`. Where a suggested panel path
  exists the emitter also writes a schema-valid `status: candidate` completed
  definition bundle; a missing suggested panel leaves only the receipt (no
  fabricated recipe).
- **META is observed-only.** Its bundle has no `derived_from` and no `complete`
  block, even though an EUR panel exists.
- **Fail closed on membership.** Every intended member must have exactly one
  resolution row and no extra row may be supplied; otherwise nothing is emitted.
- **No invented Analytical Metadata.** `plan` mode writes only the validated
  partition and a per-Store plan. `emit` mode requires a resolution table and
  refuses to write a bundle without one.

## Usage

```sh
# Tracked definitions from the config (not bundles, not in stores/).
python3 resources/generators/dbgap-mvp/generate_candidate.py definitions \
  --out-dir resources/generators/dbgap-mvp

# Metadata-only plan: writes partition.tsv and candidate-plan.yaml, no bundles.
python3 resources/generators/dbgap-mvp/generate_candidate.py plan \
  --mapping /data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/mapping.tsv \
  --out-dir /data/opengwasdb/work/epic232/221

# Fixture/authorised emit only: requires a resolution table, writes to staging,
# and runs bundle.check() on every emitted bundle.
python3 resources/generators/dbgap-mvp/generate_candidate.py emit \
  --mapping .../mapping.tsv --resolution /path/to/resolution.tsv \
  --out-dir /data/opengwasdb/work/epic232/221
```

A real metadata emit / full resolution stage requires the parent's go-ahead;
until then `emit` is exercised with fixtures only. Human acceptance, membership
freezing, reference qualification and all execution remain the parent's
responsibility.
