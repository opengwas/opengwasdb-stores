# Identity migration: FinnGen `OGS-00016` → `OGS-00017`

**Status:** done in the registry; the built Store's artifact relocation is a
separate operational step. No association/index byte was rewritten.
**Owner decision:** 2026-10-13, "release bundles merged to dev, builds running on
dev, MVP authorised, reorder the IDs."
**Authority:** [ADR 0022](adr/0022-flat-opaque-store-ids.md)'s
owner-authorized-renumbering amendment (amending
[ADR 0030](adr/0030-artifact-path-is-the-identifier-migration-retired.md)).

## Why

The MVP META Store is observed-only and cross-ancestry and must sit **ahead of**
FinnGen in the intended ordering. Placing META at `OGS-00016` required moving
the existing FinnGen bundle out of that identifier. The owner authorized the
reordering explicitly; this is not a silent reassignment.

## Mapping

| Field | Old | New |
|---|---|---|
| Store id / directory | `OGS-00016` | `OGS-00017` |
| Label | `r13-full` | `r13-full` (unchanged) |
| META observed-only | — | `OGS-00016` |
| Observed EUR/AFR/AMR/EAS | `OGS-00012/13/14/15` | unchanged |
| Completed EUR/AFR/AMR/EAS | `OGS-00018/19/20/21` | unchanged |

## What changed (registry only)

- `git mv stores/OGS-00016 stores/OGS-00017`.
- `release.yaml:store_id` and `build.yaml:store_id` updated to `OGS-00017`.
- `release.yaml` gained a `migration` provenance block
  (`previous_store_id: OGS-00016`, reason, owner authorization) and a `notes`
  Release Erratum.
- Generated views regenerated: `stores.tsv`, `STORES.md`,
  `stores/OGS-00017/summary.yaml`, and the `stores/by-label/r13-full` symlink
  (now `../OGS-00017`).
- FinnGen references elsewhere updated: `resources/generators/finngen-r13/`,
  `docs/spec/store-release-workflow.md`,
  `resources/scripts/release-benchmark-anchors.yaml`, the HM3 benchmark report
  and `tests/hm3-migration/`.

## What did NOT change (byte-identical)

Verified against the pre-migration blobs at `HEAD:stores/OGS-00016/`:

| Artifact | sha256 (old = new) |
|---|---|
| `analyses.tsv` (2,754 Analyses) | `5a3ac4e159ecf33437cd848fef44deb78d0a8dd11ab19ba0ab818065001fd1c0` |
| `validation.yaml` | `9550bc11c9e69c89d1fab8dadb25a516f94415ac2f6b4c917f7dee3e0868a7ec` |
| `sidecars/derivations.tsv` | `8ede5d8e21bf0b072ed9481b5d1f75e82cac5822ffe0a7397375987cf57b4603` |
| `sidecars/downloads.tsv` | `f090563b5d1f64be23ae980bf93bde2c3e52cef36017a701cc24374500d77905` |
| `sidecars/selection.tsv` | `28e8531ea2882218e9ddd2775a81f9461fa0af34a24b6282b67553b41188b5e2` |

No association body, index or published Store byte was read or rewritten.

## Operational follow-up (not performed here)

The built Store lives under the artifact root. ADR 0022 makes its path a pure
function of the identifier, so it must be relocated from
`<artifact-root>/OGS-00016/` to `<artifact-root>/OGS-00017/` before the new
identity is built or queried. That is an operational step owned by the parent;
no active Store was touched by this change.

## Rejected alternatives

- **Silent reassignment / directory rename alone.** No recorded mapping or
  evidence, and two releases could claim one identifier.
- **Reusing `OGS-00016` for META without moving FinnGen.** Artifact-path
  collision and ambiguous identity.
- **Reading the `migration` block as an artifact path.** Superseded by ADR 0030;
  `store_uri` stays identifier-derived.
