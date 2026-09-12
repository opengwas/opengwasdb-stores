# Evolve build.yaml in place with CLI build commands and opaque arguments

A Store Release's `build.yaml` becomes executable: it carries the fixed input
(`source.root`, `source.analyses`), the OpenGWASDB operation as an `opengwasdb`
CLI subcommand, and the optional rho and Reference-Completion branches.
`build.command` names that subcommand; `build.arguments` is an **opaque** flag
mapping passed through to it unchanged. The schema evolves the pre-existing
`build.yaml` in place - one filename, one schema - rather than introducing a
second incompatible file. Issue #97 migrates the already-checked-in releases;
until then nothing depends on the migration having landed.

The operation is the shipped CLI, not the dotted Python entrypoint the previous
`builder.entrypoint` key held. That key was inert: something still had to import
and call it, which is what the copy-pasted Store-Family adapters did (#82).
`opengwasdb` already ships a stable Typer CLI covering every phase of the
workflow, so naming the subcommand makes the configuration executable on its
own. Deliberately omitting a semantic argument schema matters for the same
reason: a newly required builder flag (for example `build-dense-vcf`'s required
`store-id`/`release-id` and its EAF-orientation gate) is absorbed by the
passthrough without a configuration change, and no key such as `chunk_shape` or
`workers` has to be kept in step with the builder's own defaults.

`resources/lib/release_plan.py` loads and validates a release's plan before
anything expensive runs, reporting pass/fail with a reason naming the offending
key. It accepts both the CLI schema and the pre-existing legacy schema, so an
unmigrated release is never rejected; a legacy plan is reported with an explicit
warning and is not executable by command name. Validation refuses an unknown
`build.command`, rho on a layout with no rho implementation (rho is Dense-only;
there is no Hybrid or Ragged rho), a Reference Resource pointer that does not
resolve to a declaration in the release's own `reference_resources`, and a
selected source file that is missing or whose declared checksum does not match.

`source.root` and `source.analyses` are resolved relative to the release
directory unless absolute, and each source path named in `analyses.tsv` is
resolved relative to `source.root` unless absolute, so existing absolute-path
manifests keep working and a release-bundle-relative `analyses.tsv` sits beside
its `build.yaml`. This keeps `build.yaml` the single executable description of a
Store Release without weakening the fixed-input boundary (ADR 0003) or the
registry's ownership of accepted release definitions (ADR 0015).
