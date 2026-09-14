# Build YAML reconciliation tests

Validates that each of the seven Trial Store Releases (`stores/OGS-00001` .. `stores/OGS-00007`)
conforms to the command-line seam (ADR 0023) and reconciles against the real active `opengwasdb`
CLI (pinned to `dev` SHA `a9e8bc8` via issue #106):

1. **Dynamic CLI Introspection:** Commands (`build.command`, `complete.command`, and derived `post` steps)
   are discovered directly from the active `opengwasdb.cli` command tree without hand-maintained tables.
2. **Live Option Flag Verification:** Option keys under `build.options`/`complete.options` are checked
   directly against the active CLI's option flags, including `--source-assembly`,
   `--source-reader-capability`, and `--analyses`.
3. **Canonical Manifest Column Contracts:** Required columns in `analyses.tsv` are validated
   directly through `opengwasdb`'s public column resolvers (`resolve_manifest_columns`, `require_columns`),
   manifest table readers (`read_analyses`, `validate_analyses`), and probe readers (`read_epi`),
   confirming that canonical ADR 0034 names (`analysis_id`, `source_file`, `stored_effect_scale`,
   `original_sd_method`, `sample_size`, `analysis_index`) are consumed as-is without registry-side projection.
4. **Derived Executability:** Derives and verifies that all seven store releases are executable today
   against the active pinned environment.
5. **Assembly Mismatch Resolution:** Confirms that for Dense and Hybrid releases (`OGS-00003`,
   `OGS-00004`, `OGS-00005`), `source-assembly: hg38` is supplied via `build.yaml` options, with the
   normalized option value matching every row's `source_genome_build` in `analyses.tsv`.
