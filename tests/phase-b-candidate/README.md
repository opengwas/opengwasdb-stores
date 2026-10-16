# Phase B candidate workflow test suite

Test suite for the resumable Phase B candidate workflow (issue #153), owned by
[`src/manifest_generation/candidate_workflow.py`](../../src/manifest_generation/candidate_workflow.py)
and driven by
[`src/manifest_generation/generate_candidate.py`](../../src/manifest_generation/generate_candidate.py)
(`python3 -m manifest_generation.generate_candidate`, `pixi run generate-candidate`).
See [`docs/spec/store-release-workflow.md`](../../docs/spec/store-release-workflow.md)
(Phase B) and the family README for the operator workflow.

## How it stays hermetic

The suite builds a temporary mirror, candidate metadata table, frozen Source
Inventory and config, and drives the real operator entry point as a subprocess.
`fixtures/fake_resolver.py` stands in for `opengwasdb resolve-analyses`: it
reproduces only the resolver's *contract* (atomic per-Analysis records, a
deterministic `index.json` in manifest order, fingerprint inputs, `--resume`
semantics, and an optional simulated interruption). It computes no ancestry,
alignment or SD — those stay OpenGWASDB's and are exercised upstream.

## Contracts and invariants covered

1. **Mixed study designs and every controlled exclusion** — quantitative rows on
   the computable SD tier, case-control rows on `log_or`/`binary_trait` with
   counts, and explicit exclusions (`ancestry_unassigned`, `ancestry_not_eur`,
   `orientation_failure`, `sd_no_qualifying_evidence`, `resolution_failed`,
   `missing_case_control_counts`) each with a machine-checkable reason.
2. **Duplicate content is surfaced, not collapsed** — both accessions stay in
   membership and appear in `sidecars/source_readiness.tsv` and a warning.
3. **Malformed sources are isolated** — one `controlled_failure` record excludes
   just that Analysis instead of aborting the batch.
4. **Every selected Analysis is accounted** — one ancestry row and one
   SD-estimation row per selected Analysis; the 6,035-row-style inventory
   evidence stays distinct from membership.
5. **Accounting failures fail finalisation before replacement** — missing, extra,
   duplicate and incompatible-`record_schema_version` records (unit and
   end-to-end).
6. **The resolution receipt closes the stale-record hole** — the receipt binds
   the contract and every record digest; changing a gate, the ancestry reference
   or fine-group map content, the resolver revision, or forging an
   internally-consistent record with a recomputed self-digest all make
   `verify`/`emit` fail and preserve a prior candidate.
   A missing receipt is rejected too. (`--cores` and `--resume` deliberately do
   not change the contract.)
7. **Resume reuses unchanged records** and reproduces byte-identical tables and
   sidecars.
8. **Interruption is safe** — a killed resolver or a failed finalisation leaves a
   prior candidate byte-identical and creates no partial one. How the bundle is
   staged and swapped into `stores/` is the shared emitter's, tested in
   `tests/emit` (#248).
9. **Byte equivalence across worker counts** — 1 worker and many workers produce
   identical `analyses.tsv` and sidecars.
10. **The contract holds** — the emitted `analyses.tsv` passes the pinned
    OpenGWASDB Analysis schema and the whole bundle passes `bundle.check()`, and
    `release.yaml` binds the frozen inventory checksum, the #152 policy and the
    executed resolver argv while staying `status: candidate`.
11. **`workflow/generate.smk` parses and wires every stage** — an automated
    hermetic `snakemake --dry-run` (skipped when `snakemake` is not on PATH, as
    in the default environment) plus a check that missing required config fails
    loudly.
12. **The Store composition rule is an emit-time gate** — when
    `store_composition.require_maf_filtered` is on, an otherwise admissible
    Analysis with no applied MAF floor is excluded as `not_maf_filtered` while
    an Analysis excluded for any other reason keeps that reason; the
    `store_composition` block records what the gate saw; and toggling the rule
    changes no resolver manifest or resolution receipt, so the rule stays
    outside the resolution contract.
13. **Quality Checks** (`test_quality_checks.py`, #285) — every measured
    exclusion reason is declared by exactly one check with its kind and default
    threshold; the policy over a fixture record set reproduces the pre-#285
    `exclusions.tsv` and `analyses.tsv` decisions, pinned as literals derived
    from the base branch's code; a warning check registered from the test
    alone reaches `validation.yaml` and `sidecars/quality_checks.tsv` with no
    policy change; the resolution receipt records the effective thresholds,
    and changing an override or a shared default makes it stale; an
    `invalid` measurement is recorded where it decides nothing and stops the
    run where it would; an `unmeasured` exclusion refuses to include the
    Analysis; and the sidecar has one row per Analysis, passing and excluded
    alike.
14. **One vocabulary** — the workflow takes the super-population codes from
    the contract (`ogstores.bundle`) and the imputation score kinds from
    Manifest Generation's rules (`generation_rules`), never its own copy (#250).

## Running the suite

```sh
pixi run --environment dev python3 tests/phase-b-candidate/test_phase_b_candidate.py
# or through the repo orchestrator
pixi run test-python
```
