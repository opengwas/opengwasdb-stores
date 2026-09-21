# Workflows

Two workflows, deliberately separate, meeting at the accepted Release Bundle.

| | | |
|---|---|---|
| `Snakefile` | Phase A | accepted bundle -> validated Store Release |
| `generate.smk` | Phase B | frozen Source Inventory -> candidate bundle (coarse wiring; the operator entry point is `pixi run generate-candidate`) |

They share no DAG. The reason is not that one graph would be complex: the
accepted bundle is a boundary *only because a human froze it*. Span both
phases with one DAG and Snakemake will correctly, silently, regenerate a
bundle and rebuild a 71 GB Store because a generator config changed upstream,
and the acceptance gate stops existing.

## Phase A Operator Interface

Phase A is driven by `workflow/Snakefile`:

```sh
pixi run release OGS-00003                                 # one registered release, plus any parent it needs
pixi run release OGS-00003 OGS-00004                       # several registered releases; lineage order is resolved
pixi run index                                             # regenerate master list, summaries, by-label/
```

A release target must be an ID currently registered under `stores/`; use
`stores.tsv` to find valid IDs. IDs shown in commands are real targets, not
placeholders. Post-steps follow the selected command's Store-format support:
`rho` is Dense-only, and `overview` is Dense/Hybrid-only because Ragged's
closed Store envelope excludes `overview.html`.

### Production Execution vs. Fixture-Scale Tests

- **Workflow tests (`tests/workflow/`) are fixture-scale**: they exercise DAG wiring, command line composition, transaction staging (`.partial`), publication gating, and step resumption using temporary mock bundles and mock executables without requiring multi-gigabyte production data or reference panels.
- **Production builds require source and reference preflight**: before executing a real release pipeline, the operator must verify that all declared raw sources (e.g. GWAS-SSF, VCF, or BESD prefixes) and reference panels (e.g. LD panels, reference allele frequency tables) exist at their configured paths.
- **The all-seven command**:
  ```sh
  pixi run release OGS-00001 OGS-00002 OGS-00003 OGS-00004 OGS-00005 OGS-00006 OGS-00007
  ```
  **Must NOT be run until configured inputs exist.** Running this without the required source data and reference resources present will fail at preflight or step execution.

See `docs/spec/store-release-workflow.md`.

## Phase B Operator Interface

The Phase B candidate workflow (issue #153) is driven by
`resources/generators/gwas-catalog-eur-hybrid/generate_candidate.py`, exposed as
one Pixi task:

```sh
pixi run generate-candidate OGS-00011 \
  --config resources/generators/gwas-catalog-eur-hybrid/config-full.yaml \
  --cores 64 \
  --resume
```

See the family README for the stage (`--stage`), resume, finalise and human-review
details. `workflow/generate.smk` wires the same stages as an optional coarse DAG
for a future multi-release batch:

```sh
snakemake --snakefile workflow/generate.smk --cores 64 \
  --config store_id=OGS-00011 \
           config=resources/generators/gwas-catalog-eur-hybrid/config-full.yaml \
           snapshot_id=gwas-catalog-ssf-eur-hybrid-2026-09-10 \
           work_root=/data/opengwasdb/work/gwas-catalog-eur-hybrid \
           registry_root=stores
```

The per-Analysis pool and checkpoints stay inside the resolver
(`opengwasdb resolve-analyses`), so the DAG never loads a genome-scale reference
once per Analysis.
