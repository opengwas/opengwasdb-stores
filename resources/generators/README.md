# Manifest Generators (Phase B)

Phase B produces a candidate Release Bundle from a Source Collection. It is
**not designed yet** -- see `docs/spec/store-release-workflow.md`, which fixes
four rules for its boundary and leaves everything inside them open.

```text
<family-id>/       family-scoped entry point, config, and README
lib/               shared helpers
lib/source-formats/    source-format-scoped generation code
```

The entry point is family-scoped, matching `CONTEXT.md`'s definition of a
Manifest Generator. The library is source-format-scoped, so two families
sharing a Source Collection share selection code without a configuration
system by accident.

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

`generation-*.yaml` files here were lifted verbatim out of the pre-ADR-0022
`build.yaml` files during migration: source reading, effect-scale validation,
ancestry assignment, and QC-panel settings are Phase B inputs, not Store build
parameters. They are retained for provenance and will be reshaped when Phase B
is designed.
