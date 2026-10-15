# Manifest Generation inputs (Phase B)

Phase B, Manifest Generation, produces a candidate Release Bundle from a Source
Collection. Its machinery is the `manifest_generation` package under
[`src/manifest_generation/`](../../src/manifest_generation/) (#249); this
directory holds its **inputs**:

```text
<generator>/           a generator's configs, reviewed tables, generation-*.yaml
                       receipts and README
<family-id>/generate.R legacy R generators, until epic #282 retires them
lib/*.R                the legacy R generators' shared helpers
lib/metadata_resolvers/    their metadata resolvers
lib/source-formats/    their source-format-scoped code
```

Recorded config paths and receipts resolve here, so these files do not move.
The code that reads them:

```text
src/manifest_generation/              the shared pipeline: candidate workflow,
                                      generate_candidate (entry point), inventory
                                      (freeze/preflight), emit, generation_rules
src/manifest_generation/providers/    per-Source-Provider connectors:
  gwas_catalog/   EBI downloads; imputation-score declarations
  ukb_b/          Source Inventory, provider metadata, candidate generator
  dbgap/          phs002453 acquisition; mvp/ the MVP candidate generator and pilot
  finngen/        R13 acquisition and assessment
  eqtlgen/        BESD generator and subsetter
  opengwas/       GWAS-VCF annotation
```

Run any of it as a module, `python3 -m manifest_generation.<module>`, or through
its Pixi task (`pixi run generate-candidate`, `preflight`, `inventory-freeze`,
`concordance-sample`, `concordance-run`). See ADR 0036 for where this is going:
one shared pipeline fed by per-source connector files.

## The rules that are already fixed

1. **Phase B is a separate workflow** from the Store build, meeting it at the
   accepted bundle.
2. **A generator's only output is a bundle directory. It never builds a
   Store.** The four `build-store.py` adapters that used to sit under
   `lib/source-formats/` are deleted; under ADR 0023 nothing but the workflow
   may reach a builder.
3. **Acquisition is separate from selection.** Acquisition is per Source
   Collection and is the expensive, resumable part.
4. **Phase B owns every `analyses.tsv` column**, including Ancestry Assignment
   and effect-scale resolution -- but it *invokes* the statistics rather than
   implementing them. If two Store Families computing something differently
   would be a bug, `opengwasdb` implements it; the registry keeps which
   Analyses, which method tier, and what tolerance to accept.
5. **Every generator emits through one emitter** (#248, ADR 0035).
   `manifest_generation.emit` is the only code that writes a candidate into
   `stores/<id>/`.
   A Python generator calls `emit()`. A legacy (R or BESD) generator keeps a
   family-shaped working bundle in its work root,
   `<artifact-root>/<output.store_id>/generate`
   (`python3 -m manifest_generation.emit work-root <id>`), and its
   `--mode=emit-registry` projects the current contract from that and its
   config's `build:` block through `manifest_generation.emit project`
   (`lib/emit_registry.R`).
   No generator config names a `families/` path as output or work root. The emitter writes `status: candidate` and the
   generator commands, checks the bundle against the Release Bundle contract,
   writes its `summary.yaml`, and replaces `stores/<id>/` in one step. It
   replaces only an earlier candidate, refuses a bundle that has been accepted
   or gone further, and runs one emit of an id at a time.

`generation-*.yaml` files here were lifted verbatim out of the pre-ADR-0022
`build.yaml` files during migration: source reading, effect-scale validation,
ancestry assignment, and QC-panel settings are Phase B inputs, not Store build
parameters. They are retained for provenance and will be reshaped when Phase B
is designed.
