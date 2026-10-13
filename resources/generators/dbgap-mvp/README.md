# MVP ancestry and META candidate emitter (#221)

This directory plans and emits the **separate single-ancestry and cross-ancestry
META Release Bundles** for the dbGaP `phs002453` acquisition of PMID 39024449
(Verma et al., VA Million Veteran Program). It is Phase B tooling only.

It never accepts, registers, builds, completes or publishes a Store, and it
never overwrites an existing bundle. Its output is a set of `status: candidate`
bundles with the human-acceptance field explicitly **pending**.

## The partition

The dbGaP mapping marks each Analysis with the ancestry parsed from its member
name; `META` is the cross-ancestry bucket. The legacy candidate table still
groups META under the European `store_key`, so this tool makes the split
explicit and validates it against the accepted immutable counts:

| Bucket | Analyses | Candidate store key |
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

## Identity allocation

Recommended plan (see `config-candidate.yaml`); it does **not** reuse or reassign
any existing identifier:

| Store | Observed | Completed child |
|---|---|---|
| EUR | OGS-00012 | OGS-00018 |
| AFR | OGS-00013 | OGS-00019 |
| AMR | OGS-00014 | OGS-00020 (plan only; reference on hold) |
| EAS | OGS-00015 | OGS-00021 |
| META (observed-only) | OGS-00017 | none |

FinnGen stays **OGS-00016**. The epic's preferred ordering (META ahead of
FinnGen) would require reusing `OGS-00016` for META, which collides with the
existing FinnGen artifact path (the artifact path is a pure function of the id)
and reassigns an `OGS-` identifier that ADR 0022 says is never reassigned. That
is left to an explicit owner decision and an ADR amendment; `config-candidate.yaml`
records `finngen.renumbered_to: null`.

## Policy invariants

- **META is observed-only.** Its bundle has no `derived_from` and no `complete`
  block, even though an EUR panel exists. The emitter enforces this, it is not
  only documented.
- **Completion is reference-gated.** A lineage-linked completed child is emitted
  only when its `completion_reference.status` is `qualified`. AMR has no declared
  AMR LD Reference Resource, so it produces `<child>.completion-plan.yaml` with
  `status: hold` and no buildable bundle. A missing reference is a blocker, not
  an observed-only success.
- **Fail closed on membership.** Every intended member must have exactly one
  resolution row and no extra row may be supplied; otherwise nothing is emitted.
- **No invented Analytical Metadata.** `plan` mode writes only the validated
  partition and a per-Store plan. `emit` mode requires a resolution table and
  refuses to write a bundle without one.

## Usage

```sh
# Metadata-only plan: writes partition.tsv and candidate-plan.yaml, no bundles.
python3 resources/generators/dbgap-mvp/generate_candidate.py plan \
  --mapping /data/opengwasdb/work/epic232/220/conversion-retry-20261012T223021Z/mapping.tsv \
  --out-dir /data/opengwasdb/work/epic232/221

# Emit candidates to a staging directory (never stores/) once a resolution
# table of required Analysis metadata exists.
python3 resources/generators/dbgap-mvp/generate_candidate.py emit \
  --mapping .../mapping.tsv --resolution /path/to/resolution.tsv \
  --out-dir /data/opengwasdb/work/epic232/221
```

`emit` runs `bundle.check()` on every emitted bundle and refuses to leave a
bundle that fails the registry contract. Human acceptance, final membership
freezing and all execution remain the parent's responsibility.
