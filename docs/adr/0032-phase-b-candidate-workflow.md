# Phase B candidate generation invokes the OpenGWASDB resolver

Implements the first concrete Phase B Manifest Generator for the EBI GWAS
Catalog `hybrid__European` release (issue #153), on the boundary ADR 0012,
ADR 0017 and ADR 0023 already fixed.

Acquisition had produced a ~1.8 TB mirror and a frozen Source Inventory
(issue #151); the ancestry and reference-AF policy decisions were settled in
issue #152. What remained was the workflow that turns those into a candidate
Release Bundle without building a Store and without re-implementing statistics
OpenGWASDB already owns.

## Decision

**Expose one thin registry-owned candidate workflow that invokes
`opengwasdb resolve-analyses`.** The entry point is
`resources/generators/gwas-catalog-eur-hybrid/generate_candidate.py`, run as
`pixi run generate-candidate OGS-xxxxx --config <config> --cores N [--resume]`.
It runs five stages — `preflight`, `prepare`, `resolve`, `verify`, `emit` — and
`workflow/generate.smk` wires them as an optional coarse DAG.

- **The resolver owns the statistics and the durable work.**
  `opengwasdb resolve-analyses` (opengwasdb#208) loads the genome-scale ancestry
  reference once, owns the process pool, and writes atomic, fingerprint-aware
  per-Analysis records with `--resume`. The registry never loads a reference,
  forks a worker, computes an ancestry, aligns an allele, or estimates a
  phenotype SD. That is ADR 0012's "orchestration only" applied literally; the
  upstream commit is pinned by immutable revision in `pixi.toml` rather than
  vendored (ADR 0023).

- **The registry owns membership and the acceptance policy.** From the frozen
  inventory's exact `data_file` paths and the config's `defaults.by_study_design`
  tiers it derives the canonical resolver manifest, and from the records it
  decides membership: which Analyses are in the candidate, which are
  `exclude_from_build` audit rows, and why. The controlled exclusion vocabulary
  is `resolution_failed`, `ancestry_unassigned`, `ancestry_not_eur`,
  `orientation_failure`, `sd_no_reference_resource_for_ancestry`,
  `sd_no_qualifying_evidence`, `sd_no_usable_sample_size`, `sd_failed`,
  `missing_sample_size` and `missing_case_control_counts`; each is explained in
  `sidecars/exclusions.tsv`.

- **Every record is accounted before anything is replaced.** Finalisation refuses
  a selected Analysis with no record, a record that names an Analysis the
  manifest does not, a duplicate record, a record declaring an incompatible
  `record_schema_version`, or a record whose source identity, source file or
  method tier no longer matches the manifest resolved. The fingerprint digest is
  recomputed over the record's own inputs.

- **A resolution receipt closes the stale-record hole a self-digest cannot.**
  A record's own fingerprint digest only proves it is *internally* consistent: a
  record produced under different gates, ancestry reference or fine-group map,
  extraction panel, reference-AF resources or tool revision still recomputes a
  valid self-digest. After a successful resolver invocation the workflow
  therefore atomically writes `resolver/resolution_receipt.json`, binding the
  resolver manifest's checksum, the registry-recomputable slice of the
  resolution contract (declared gates, MAF floor, reader capability, extraction
  panel, ancestry reference/group-map and reference-AF resource content
  fingerprints), the installed resolver's content identity, and every
  `analysis_id` to the fingerprint digest the resolver wrote for it. Standalone
  `verify`/`emit` recompute the contract from the current config, references and
  installed tool and require it to equal the receipt, and require every record
  digest to match the receipt. The registry never re-derives the resolver's
  statistics; it binds and re-checks the inputs that determine them. Core count
  and `--resume` are recorded as evidence but are deliberately *not* part of the
  contract, so neither changes the contract or the bundle tables/sidecars.

- **Output is a candidate only, published atomically.** The bundle is written
  under a hidden staging sibling, checked with `bundle.check()` and the pinned
  OpenGWASDB Analysis schema, and renamed into `stores/<id>/` with the previous
  candidate restored if the rename fails. The workflow never writes an `accepted`
  status and never invokes Phase A.

- **Resolved metadata is joined, not re-selected.** The candidate table
  (`resources/scripts/ebi-studies.r`'s output, already checksummed by the #151
  freeze) supplies the labels, publication identity, total N and the
  case/control counts the schema requires for `log_or` rows. Membership remains
  the frozen inventory's; the join cannot add or drop a member.

## Consequences

- `release.yaml` records the operator invocation and the exact
  `opengwasdb resolve-analyses` argv in `generator.commands`, plus the frozen
  inventory checksum and the preflight report path, so a reviewer can reproduce
  the candidate's provenance.
- `analyses.tsv` contains included rows **and** the `exclude_from_build` audit
  rows, honouring ADR 0025: `bundle.check()` and Phase A's derived build
  manifest both drop the excluded rows, and the reason stays reviewable.
- Aggregation is ordered by the frozen inventory and sidecar numerics go through
  the issue-#143 formatter, so 1 worker and 64 workers produce byte-identical
  tables and sidecars, and a resumed run reproduces an uninterrupted run's bytes.
- The resolution receipt is written after a successful `resolve` and is required
  by standalone `verify`/`emit`; a changed gate, reference, fine-group map,
  extraction panel, reference-AF resource or resolver revision makes it stale
  and forces a re-resolve rather than freezing stale Analyses into a candidate.
- The Hybrid Build Recipe's Dense-Component axis (`--variant-reference` /
  `--reference-panel`) is deliberately not declared in `config-full.yaml`: it is
  a Reference Resource decision for acceptance, and inventing a host path would
  bind the candidate to one machine. Acceptance adds one before the Store is
  built.
- This does not change Phase A, which still consumes only the accepted bundle.

See [store-release-workflow.md](../spec/store-release-workflow.md) (Phase B),
[release-metadata-schema.md](../release-metadata-schema.md) (sidecars), and the
`gwas-catalog-eur-hybrid` README.
