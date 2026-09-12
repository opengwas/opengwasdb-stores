# Catalogue-routed Hybrid pilot: `gwas-catalog-eur-hybrid/eur-hybrid-pilot-10` (issue #104)

Status: **DONE** — `gwas-catalog-eur-hybrid/eur-hybrid-pilot-10` is the first
release built through the catalogue-routed pre-build path: `assign-ancestry` and
`route-catalogue` produce a routed Analysis Catalogue, and
`build-hybrid-from-catalogue` builds a Hybrid Store from it. The whole run is one
command, with no Store-Family-specific build script involved.

This is the evidence record for issue #104; it is written to be posted to the
issue. It follows the shape of the issue #101 FinnGen evidence record.

## The one command

```bash
pixi run release --configfile families/gwas-catalog-eur-hybrid/releases/eur-hybrid-pilot-10/build.yaml
```

Nothing else was run by hand. The release's `build.command` is
`build-hybrid-from-catalogue`, which is what selects the catalogue path; the
Snakefile contains no Store-Family name or family conditional. The pinned
`opengwasdb` revision is `6ef919e721d5b7368acad591264be50f79b77dfb`
(`build_environment.opengwasdb_commit` in `validation.yaml`).

## Result

| | Catalogued (before) | Rebuilt (after) |
|---|---|---|
| Layout | Hybrid Observed-Only, format **0.1** | Hybrid Observed-Only, format **1.0** (`z` int16 fixed point) |
| Analyses | 10 | **8** (see below) |
| Union variants | 20,531,956 | 19,790,723 |
| Dense Component (on-panel) | 9,847,807 | 9,847,807 (identical) |
| Ragged Overflow (off-panel) | 10,684,149 | 9,942,916 |
| Overflow associations | 27,810,834 | 23,875,142 |
| Store size | 4.1G | 5,353,715,683 bytes (5.0G) |
| `validation.yaml` | `passed_with_warnings` | **`passed`**, no warnings |
| Release status | `built` | **`validated`** |

The rebuilt Store passes `opengwasdb validate`, and the read-back asserts it
carries exactly the routed `assigned_ancestry == EUR` subset.

## Routing decisions

The catalogue path makes each Analysis's routing explicit, and the rebuilt
Store's Analysis set is exactly the routing decision:

| Analysis | Assigned Ancestry | Routing | Built? | Why |
|---|---|---|---|---|
| GCST005076 | EUR (`af_assigned`) | EUR | yes | gate `ok` |
| GCST006465 | EUR (`af_assigned`) | EUR | yes | gate `ok` |
| GCST007320 | **Unassigned** | — | **no** | NNLS residual `0.137` exceeds the `0.06` gate |
| GCST007435 | EUR (`af_assigned`) | EUR | yes | gate `ok` |
| GCST009324 | EUR (`af_assigned`) | EUR | yes | gate `ok` |
| GCST009325 | EUR (`af_assigned`) | EUR | yes | gate `ok` |
| GCST009758 | EUR (`af_assigned`) | EUR | yes | gate `ok` |
| GCST009925 | EUR (`af_assigned`) | EUR | yes | gate `ok` |
| GCST009926 | EUR (`af_assigned`) | EUR | yes | gate `ok` |
| GCST003566 | (not in the Catalogue) | — | no | excluded from the build (`exclude_from_build`): its EAF is reported against the other allele (opengwasdb#115) |

Every routed Analysis passed the coverage gate: 6.8M–17.4M variants, all 22
autosomes, largest-chromosome share ≤ 0.086. The derived coverage table
(`work/catalogue-coverage.tsv`) is produced from the sources by
`route_catalogue`, not authored by hand.

**Differences from the catalogued release, explained.**

- **Two fewer Analyses.** The catalogued Store was built by the pre-#104 adapter
  from a manifest that stamped every row `assigned_ancestry = European` from
  `source_defaults` — the source's declared population, taken on trust
  (`ancestry_assignment_method = source_trusted_no_af`). The catalogue path
  derives ancestry from each Analysis's own allele frequencies instead, so it
  (a) drops `GCST003566` at input because the registry already excludes it, and
  (b) leaves `GCST007320` Unassigned because its fit residual exceeds the gate.
  Both are the intended behaviour, not defects: the trusted label was exactly
  what hid `GCST003566`'s flipped EAF column until a store was built from it.
- **The Dense Component is byte-for-byte the same axis** (9,847,807 variants,
  the same panel ALIDs). The variant-axis and Overflow counts differ only by the
  two Analyses that are absent, so the Hybrid layout was genuinely exercised and
  the routing is the only thing that changed.
- **`assigned_ancestry` is now `EUR`**, a real super-population, not the
  `European` label the source declared. `ancestry_assignment_method` is
  `af_assigned` because it genuinely was.

## Scale evidence

Measured on the full run (5 phases + build + overview + validation, the `release`
target) and on a forced rebuild of `build_observed_store` only:

- Full run wall clock: **37:58**, peak RSS **24.5 GB**.
- `build_observed_store`: 1,728 s (28 m 48 s) at 16 workers; the serial Pass 1
  union-axis scan dominates, then the fork-pool Pass 2 writes the Dense and
  Overflow components.
- `assign_ancestry`: ~3 m 51 s (9 GWAS-SSF sources, 16 workers).
- `route_catalogue`: ~2 m 33 s (coverage derivation over the 9 sources, 16
  workers, plus `route-catalogue`).
- `regenerate_observed_overview` + `validate_observed_release`: ~5 s + ~2 m 21 s.

`sidecars/build-report.tsv` records `n_analyses=8`, `n_variants=19,790,723`,
`status=passed`; `validation.yaml` records `checks.{schema,files,ancestry,store}
= passed`.

## Acceptance criteria

- **A catalogue-routed release resolves to a routed Analysis Catalogue and
  builds from it.** Yes: `build.command: build-hybrid-from-catalogue` selects the
  `assign_ancestry → route_catalogue → build_observed_store` chain; the build
  consumed `work/routed-catalogue.tsv`.
- **Ancestry assignment and catalogue routing each run as their own phase with
  their own completion record.** Yes: `work/completions/assign_ancestry.json` and
  `work/completions/route_catalogue.json`. Forcing a rebuild of
  `build_observed_store` re-ran the build and its downstream phases but left both
  records untouched (same completion timestamps), so an interrupted routing step
  cannot force a re-assignment. `tests/release-workflow/run_tests.py` proves both
  the interruption and the non-re-assignment at fixture scale.
- **The Snakefile contains no Store-Family name or family conditional.** Yes:
  the branch is `RELEASE.catalogue_routed`, derived from `build.command`. The
  `test_snakefile_is_wiring_only` assertion still passes.
- **Input validation rejects a catalogue-routed command whose required inputs
  are absent, before anything expensive runs.** Yes:
  `resources/lib/release_plan.py` refuses a missing reader capability,
  `ancestry_assignment.reference_resource_id`, its `fine_group_map`, or a
  `hybrid_dense_panel` resource, naming the offending key. Covered by
  `tests/release-plan/` and `tests/release-workflow/`.
- **A `gwas-catalog-eur-hybrid` pilot release builds through the workflow and
  validates.** Yes: `opengwasdb validate` reports `valid`; `validation.yaml`
  checks all `passed`; `release.yaml` lands `validated`.
- **The rebuilt Store's Analysis set and routing decisions match the catalogued
  release, with any difference explained.** Yes: see "Routing decisions" above.
  The differences are `GCST003566` (registry exclusion, EAF-flipped) and
  `GCST007320` (residual gate), both intended.
- **The workflow spec and operator guide describe both pre-build paths.** Yes:
  [`docs/spec/store-release-workflow.md`](spec/store-release-workflow.md)
  ("The two pre-build paths"), [`workflow/README.md`](../workflow/README.md)
  ("The catalogue-routed path"), and
  [`docs/operator-guide.md`](operator-guide.md) (§4a).

## An upstream gap the pilot surfaced

`opengwasdb`'s Analysis Catalogue is a fixed column set. It carries
`source_reader_capability` (opengwasdb#115) but not `source_assembly`, so
`build-hybrid-from-catalogue`'s row-filtered manifest omits it and the builder
defaults every row to hg19 — re-lifting an already-GRCh38 source and failing the
liftover gate, exactly the opengwasdb#85 failure the manifest-direct path
avoids by sourcing capability/assembly from `build.yaml`. `workflow/phase.py`
restores that parity for the catalogue path: `route_catalogue` fills
`source_assembly` onto the routed Catalogue from the release's
`normalisation.source_assembly`, so the build passes it through unchanged. An
upstream fix (add `source_assembly` to the Catalogue's annotation columns) would
let that fill become a no-op.

## Reproduce

Artifacts (external, not in Git, per ADR 0015):

- rebuilt Store:
  `/data/opengwasdb/gwas-catalog-eur-hybrid/releases/eur-hybrid-pilot-10/store/hybrid__European__pilot10-cc`
- pre-#104 catalogued format-0.1 Store and the release bundle, retained:
  `/data/opengwasdb/backups/issue-104/` (verified byte-identical to the source
  Store before the rebuild: identical file count, total bytes and tree digest
  `fa0dd176…355b4`)
- captured semantic/routing baseline of the catalogued Store:
  `/data/opengwasdb/backups/issue-104/baseline-catalogued.json`

The small registry artifacts the rebuild wrote back into the repository are the
release's `validation.yaml`, its `sidecars/` (`input-validation.json`,
`catalogue-assignment.json`, `catalogue-routing.json`, `build-report.tsv`), and
the one-line Release Status in `release.yaml`. No Store or raw data is tracked.
