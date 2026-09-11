# Snakemake Store Release Pipeline prototype

**THROWAWAY PROTOTYPE — issue 86. This is not production workflow code.**

## Question

Can one fixed release input (`build.yaml`, the selected `analyses.tsv`, and the
raw summary-statistics directory they describe) drive a comprehensible,
resumable Store build without putting Store Family-specific logic in the
Snakefile?

The prototype performs no scientific computation. Each rule emits tiny stand-in
Store files, reports, and a completion record under
`.prototype-state/snakemake-release-pipeline/` (safe to remove). It deliberately
enables rho and Reference Completion so both optional branches remain visible.

## Run

From the repository root:

```bash
pixi run -e workflow-prototype prototype-release-pipeline
```

In the TUI, use `d` to inspect the DAG, `i` to interrupt the observed build,
then `f` to see Snakemake resume it and complete both releases.

The equivalent direct command is:

```bash
pixi run -e workflow-prototype snakemake \
  --snakefile resources/prototypes/snakemake-release-pipeline/Snakefile \
  --configfile resources/prototypes/snakemake-release-pipeline/fixtures/build.yaml \
  --cores 1 full_release
```

## Architectural claim represented

- Store-specific helper scripts stop at the fixed-input boundary. They may
  download, select, and describe inputs, but the core DAG always receives the
  same three things.
- `build.yaml` names the `analyses.tsv`, raw directory, reader capability and
  reader options, OpenGWASDB operation and arguments, reference resources, rho
  settings, and optional Reference Completion descendant.
- The OpenGWASDB build produces the Store envelope, initial `overview.html`, and
  Top-Hit indexes. Rho is a subsequent in-place operation, followed by explicit
  overview regeneration so the Rho tab is present.
- Reference Completion creates a lineage-linked child Store Release; it never
  mutates the observed parent.
- Snakemake tracks small completion records for expensive or in-place phases,
  so an interrupted build can resume without treating a partial Store as done.

The proposed durable contract and file/process DAG are documented in
`docs/spec/store-release-workflow.md`.
