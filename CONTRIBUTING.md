# Contributing to opengwasdb-stores

This repository is the registry and orchestration layer for OpenGWASDB Store
Releases. It records what should be built and the evidence that it was built
correctly; reusable source readers, Store builders, and query behavior belong in
the sibling `opengwasdb` repository.

`CONTEXT.md` is the authority on project vocabulary. Use its terms in code,
configuration, documentation, issue comments, and commit messages.

## The principle everything else serves

**A wrong answer that looks like a right answer is the worst outcome this
project can produce.**

Store construction can fail silently: a source column can be interpreted as the
wrong allele, a file can be omitted, or a partial Store can look complete. Three
habits follow:

1. Fail loudly rather than silently skipping, coercing, or substituting data.
2. Keep absence distinct from zero and from a reference-derived value.
3. Add a validation rule whenever a new silent-failure class is discovered.

## Branches

```text
feature branch  ->  dev  ->  main
```

- `main` is the stable repository state.
- `dev` is the integration branch. All work lands here before `main`.
- Feature branches start from `dev` and return to it, one issue or coherent
  change at a time.

Do not mix unrelated clean-up into a feature branch. Preserve a contributor's
existing working-tree changes and resolve overlapping edits explicitly.

## Repository boundaries

The repository tracks small, reviewable definitions and evidence:

- Source Collection, Store Family, and Reference Resource metadata;
- accepted Release Manifest bundles (`release.yaml`, `analyses.tsv`,
  `build.yaml`, and `validation.yaml`);
- Store-specific input-generation helpers and shared orchestration;
- small validation and build reports.

Raw summary statistics, built Stores, transient work directories, and large logs
live outside Git and are referenced by explicit paths or URIs. Shared source
reading, normalization, Store construction, and validation behavior belongs in
`opengwasdb` (ADRs 0012 and 0017).

Store-specific helpers may acquire and select data, but the shared Store Release
workflow begins at the fixed-input boundary documented in
[`docs/spec/store-release-workflow.md`](docs/spec/store-release-workflow.md).

## Metadata and scripts

- YAML holds nested configuration and metadata; TSV holds repeated tabular rows
  (ADR 0013).
- Keep Release Bundles self-contained and use family-first artifact paths (ADRs
  0014 and 0018).
- A committed `analyses.tsv` is an exact release selection. Do not silently add
  every file found in a directory at build time.
- Record source file names and checksums. Fail if a selected file is missing or
  its identity no longer matches.
- Do not copy OpenGWASDB builder logic into this repository. Invoke a documented
  OpenGWASDB operation with explicit arguments.
- Comments should explain why a choice exists and cite the issue or ADR that
  constrained it.

## Decisions and documentation

Anything that constrains future work gets a numbered ADR in `docs/adr/`.
Supersede an earlier ADR explicitly rather than quietly contradicting it. A
workflow or file contract also needs a specification under `docs/spec/` in the
same commit as the implementation that adopts it.

Documentation is part of a change:

- update `CONTEXT.md` when the domain language changes;
- update `docs/release-metadata-schema.md` when bundle fields change;
- update the Store Release workflow specification when its boundary or DAG
  changes;
- regenerate reports rather than hand-editing measured values;
- update the sibling `opengwasdb` repository or open an issue there when a
  change requires a new reader, builder, manifest field, or validation behavior.

## Checks

Use the Pixi environments pinned by this repository:

```bash
pixi run env-check
pixi run test
pixi run test-python
pixi run test-r
pixi run --environment docs docs-smoke
```

Run the checks relevant to a change. A behavior change to a script needs a test
that reproduces the failure it prevents. Assert that fixtures exercise the
intended source format, ancestry path, or Store layout before relying on their
result.

For a Release Bundle or generated report, also run the repository's applicable
schema and validation commands and inspect the resulting evidence. A successful
process exit is not proof that a scientifically valid Store was produced.

## Commits and pull requests

A commit message explains why the change exists, records relevant measurements
or validation, and references its issue. If a change alters a documented
contract, update the specification or ADR in the same commit.

When reporting work, state what was completed, what was deliberately excluded,
which checks ran, and any external data that was unavailable. Do not describe a
partial build or unvalidated report as complete.
