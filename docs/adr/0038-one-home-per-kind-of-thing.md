# One home per kind of thing

The repository grew three homes for Python and a fourth for everything else.
Reviewing #299, the user asked why `/curation`, `/resources` and `/src` all
exist. Decided on 21 Oct 2026 (#317):

- **Python lived in three places.** The two workflows' packages were under
  `src/` (#249). The curation package sat at the repository root, and the
  command-line tools that check bundles, audit Indexed Variant Subsets and
  render the readiness view were loose scripts in `resources/scripts/`.
- **`resources/scripts/` contradicted "resources/ is inputs".** It held the
  test runner, the bundle check, the site build and its report sources,
  benchmarks, GWAS Catalog and OpenGWAS acquisition scripts, and one
  reviewed input, the dbGaP key-discrepancy ledger. CONTRIBUTING called it
  "the one directory at risk of becoming a junk drawer", and asked people to
  move things out when they became steps. That rule is a judgement, so no
  test could check it.
- **Reference Resources kept their build scripts beside their data**, and the
  SomaScan target generators sat with the Manifest Generators.

## Decision

**Each kind of thing has one home, and a test checks it.**

```text
stores/            output: Release Bundles; stores.tsv and STORES.md beside it
src/               all importable Python
  ogstores/          Store Release Build and the Release Bundle contract, with the
                     readiness and indexed-subset audit CLIs
  manifest_generation/  the shared pipeline and providers/, with the check_bundles CLI;
                     a provider's acquisition scripts, in any language, under it
  curation/          trait-mapping curation
workflow/          Snakemake wiring only
resources/         inputs only: reference-resources/, annotations/, inventories/,
                   generators/ (release configs, reviewed tables, receipts, and the
                   legacy R generators until stage 2 retires them)
tools/             repository tooling that is neither a package nor an input
tests/             the suites, and run_all_tests.py
docs/              adr/, spec/, reports/ (report sources), and the rendered site
```

To place a file, ask what it is:
1. Something the repository produces goes in `stores/`, or in `docs/` if it
   is a report.
2. Something a workflow reads as data goes in `resources/`. That covers a
   release config, a reviewed table, a frozen inventory and a Reference
   Resource declaration.
3. Code a workflow imports or runs goes in `src/`. Python is a package
   module, run with `python3 -m`. A Source Provider's acquisition script, in
   any language, goes in its `providers/<provider>/`.
4. A test or the test runner goes in `tests/`, and a report source in
   `docs/reports/`. Anything else a person runs, to maintain the repository or
   to build a Reference Resource, goes in `tools/`.

Three rules keep it true:
- **A CLI belongs with what it applies.** Store Release Build imports nothing
  from Manifest Generation (ADR 0035). So the bundle check, which applies
  Manifest Generation's rules as well as the contract, lives in
  `manifest_generation`, though what it checks is a Release Bundle.
- **Moving a file never breaks a path generation still reads.** Those paths
  are frozen inventory provenance, the originals named there, and config
  inputs. A move that would break one re-freezes under a new snapshot id, or
  the file stays where it is. Paths recorded in `stores/` and in
  `generation-*.yaml` receipts are history and are never edited. So is a
  lineage string that records which tool changed a file.
- **`tests/packaging/test_layout.py` enforces the layout.** No importable
  Python package lives outside `src/` and `tests/`. Nothing under
  `resources/` is code, except the legacy R generators, each listed with the
  ticket that retires it. `resources/scripts/` does not come back.

## Considered options

- **Keep `resources/scripts/` with a stricter README rule.** The rule already
  existed, and the directory grew anyway. A guard needs a definition it can
  test, and "no code under `resources/`" is one.
- **Make every tool a package module.** The LD-panel toolchain, the site
  build and the benchmarks are imported by nothing, and one benchmark
  deliberately imports another checkout's `opengwasdb`. Packaging them adds
  install surface and has no consumer.
- **Move the legacy R generators too.** Stage 2 of epic #282 deletes them
  (#288, #289), and their recorded commands and receipts resolve under
  `resources/generators/`. Moving them now would break those paths for code
  that is about to go.
- **Call the tooling directory `scripts/`.** That names the form, not the
  kind, and it is what `resources/scripts/` was.

## Consequences

- `curation/` is `src/curation/`. `resources/scripts/` is gone, and each item
  has moved to the home above. The Reference Resource build and verify
  scripts and the SomaScan target generators are in
  `tools/reference-resources/<resource>/`.
- Two R helpers have no retiring ticket. GWAS Catalog's `ebi-studies.r`, in
  `providers/gwas_catalog/`, still sources `lib/metadata_resolvers/gwas_catalog_ssf.R`
  and its `contract.R`. They stay listed until that script stops sourcing
  them.
- Nothing was re-frozen. The only moved file named in frozen provenance is
  `materialise-gwas-ssf-ragged.R`, in the Chen 2023 inventory's
  `reformat_tool` lineage. Generation reads that field as text and never
  resolves it.
- An existing checkout picks up the new package location on its next
  `pixi run`, because pixi re-installs the editable package when
  `pyproject.toml` changes.
- `families/` and `.rebuild133/` are not part of the layout, and retire
  through #252.
- ADR 0011 stands. Reference Resources are build inputs. Only their build
  scripts moved, to `tools/`.
