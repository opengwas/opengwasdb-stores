# Association Coverage is declared in the Release Bundle, in one vocabulary

Association Coverage tells a reader which associations a Store Release holds. It
was wrong or missing at every step. The glossary listed six informal values; a
release config declared one, in two different spellings; and the value never
reached the Release Bundle, so it was lost before the builder and the registry
could see it. ADR 0029 had removed `association_coverage` from `release.yaml`
because no code read it. Two things have changed since, and both reverse that
reason: the Analysis Catalog reads it and shows it to users (#313, ADR 0039), and
opengwasdb#298 makes it a build input, so the value opengwasdb writes per layout
becomes a value the release declares.

## Decision

**It is declared, once, in `release.yaml`.** `association_coverage` is a required
key of the Release Bundle (`RELEASE_REQUIRED_KEYS`), and `bundle.check()` refuses
a value outside the vocabulary or a value that is not a string.
`Bundle.association_coverage` answers a reader that has already gated on
`check()`: the Analysis Catalog refuses an errored bundle before it reads any
fact. It still raises rather than answering a default, as `Bundle.trial` does,
so a reader that has not gated fails loudly instead of reading an absent or
unknown value as, say, `gwas`, which would show a layout's coverage as the
Store's (CONTRIBUTING: never substitute data).

**One vocabulary, and it is opengwasdb's too.** The owner's decision of 22 Oct
2026 sets four values, in `ogstores.bundle.ASSOCIATION_COVERAGES`:

| Value | What the Store holds |
|---|---|
| `gwas` | every association the source reports, genome-wide |
| `cis` | a cis window around each Analysis's target, and nothing else |
| `cis_and_signals` | a cis window plus significant signals elsewhere |
| `signals_only` | significant signals and their flanks, with no cis window |

`gwas` replaces the registry's `full_gwas` and opengwasdb's `full`;
`cis_and_signals` replaces `cis_plus_signals`. `cis` and `signals_only` name
Stores that were previously mislabelled by their layout.

**A region policy declares only two of them.** `cis_and_signals` and
`signals_only` (`REGION_POLICY_COVERAGES`): a policy selects regions, while `gwas`
and `cis` describe a Store built from every source row or from cis windows alone,
which no filter produces.

**A frozen manifest is read, never rewritten.** Sun 2018's Acquisition Manifest
records `cis_plus_signals` in its checksummed region-policy lineage. Rewriting it
would break `inventory_tsv_sha256` and the freeze and preflight checksums, so the
retired spelling is aliased on read, through one function
(`manifest_generation.region_filter.canonical_association_coverage`), and
preflight's "filtered to exactly this policy" comparison still matches. A legacy
`full` in a Store's `manifest.json` likewise reads as `gwas` (opengwasdb#298), so
no published Store is rewritten.

**Backfilling it is not a new Store Release.** It is a descriptive fact about a
release, so ADR 0004 (an accepted bundle is not edited) does not make the
backfill a new release, and no identity, checksum or amendment check reads it:
the amendment check compares a plan's argv and the manifest a Store was built
from, and `release.yaml` is neither. The builder receives the declared value
once opengwas/opengwasdb#298 lands and this repository's pin moves past it; that
is also when `register` can refuse a mismatch with the value opengwasdb
reports.

## Consequences

- Every committed bundle declares its true coverage; `pixi run bundle-check`
  requires it and OGS-00006 says `signals_only`.
- Manifest Generation requires `association_coverage` in every release config,
  writes it after `label:` in the candidate `release.yaml`, and refuses a config
  without one. For a region-filtered config it must equal
  `region_policy.association_coverage` exactly: a release config and a bundle
  take only the four current values, and
  `canonical_association_coverage` is only for reading frozen historical
  evidence.
- The Analysis Catalog reads `release.yaml` for `stores.association_coverage`,
  the `coverage` count dimension and the summary report; it no longer reads the
  Validation Record's `observed.association_coverage`, which stays the value
  opengwasdb reports (ADR 0039 is amended accordingly).
- docs/spec/release-config.md, docs/spec/analysis-catalog.md,
  docs/release-metadata-schema.md and CONTEXT.md's Association Coverage entry
  state the four values.

**Amends ADR 0029.** `association_coverage` is restored to `release.yaml`; the
other nine keys ADR 0029 removed stay removed, and its reasons for removing them
are untouched.
