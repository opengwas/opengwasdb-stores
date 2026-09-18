# The registry filters excluded analyses before build

`analyses.tsv` carries a documented `exclude_from_build` column: "`true` only
for rows retained for audit but intentionally skipped by the build"
([release-metadata-schema.md](../release-metadata-schema.md)). Nothing honoured
it. `plan()` handed the bundle's `analyses.tsv` to the builder verbatim, and
`opengwasdb` classifies `exclude_from_build` as a REGISTRY_ONLY column that it
strips without acting on it (opengwasdb ADR 0034), so an excluded row was built
anyway.

This was not hypothetical. `OGS-00004` row 0, `GCST003566`, is flagged
`exclude_from_build=true` because its `effect_allele_frequency` is reported
against the other allele. It was built regardless, and opengwasdb's EAF
consensus check aborted the build 45 minutes in with `EafOrientationError`
(r = -0.9954). Orienting the study to canonical A1 and comparing against
`GCST005076` gives r = -0.9994 over 277,266 shared non-palindromic variants:
the exclusion is correct, and the registry ignored it. Three more such rows sit
in `families/ukb-b/releases/dense-observed-vcf-c128-rebuild117/analyses.tsv`,
waiting to abort the same build the same way.

## Decision

The registry materialises a **derived build manifest** and points every
build-phase command at it. The bundle keeps the audit row.

`ogstores.manifest.materialise_build_manifest()` reads the bundle's
`analyses.tsv` and writes `<artifact-root>/<store_id>/work/analyses.tsv`:

- every row whose `exclude_from_build` is `true` (case-insensitive, whitespace
  trimmed) is dropped;
- every other column and the surviving rows' order are preserved verbatim;
- `analysis_index` is re-densified to `0..n-1` over the survivors, when that
  column exists;
- no exclusion decision is written into any other column;
- a sidecar JSON records every dropped `analysis_id` and its
  `inclusion_reason`, so the audit row is explained rather than silently
  discarded;
- both files are written atomically (temp file, `os.replace`, directory fsync),
  like `run.py`'s step records.

It fails loudly rather than degrading: a malformed `exclude_from_build` value
names the offending row and value; an all-excluded manifest, a header-only
manifest, or a missing `analysis_id` column raises and writes nothing.

`plan()` stays pure. It opens no `analyses.tsv`; it only constructs the derived
path. The `analyses` token — positional for Dense/Hybrid/Ragged-SSF, and the
`--analyses` flag for Ragged BESD — resolves to the derived path, and the
step's declared `inputs` name it too, so the workflow builds the manifest before
the builder runs. Completion (`complete-*`) commands consume only their parent
Store and take no analyses manifest. The `Snakefile` gets one
`build_manifest` rule that calls `ogstores.manifest`; per ADR 0023 it carries no
filtering logic of its own.

`register` counts the derived manifest, not the bundle table, for its
`n_analyses` fallback; `index`'s derived `build_command` names the derived path.

## Rejected alternatives

**Delete the excluded row from the bundle.** Rejected: the bundle is the audit
record, and the reason a study is absent from a Store is exactly what a reviewer
needs to see. The row carries a dated `inclusion_reason` naming the evidence
(allele orientation, r values, the upstream issue). Deleting it makes an
absence unexplainable and is a material change to release membership, which
ADR 0004 makes a new Store Release rather than an edit. The bundle also has to
remain a faithful record of what was selected even when selection is later
overridden.

**Teach `opengwasdb` to honour `exclude_from_build`.** Rejected: opengwasdb
ADR 0034 deliberately classifies this column as registry-only and strips it.
Teaching every layout's builder to branch on a registry audit field would move
the registry's release-selection semantics into the shared builder, where they
would have to be re-implemented and kept consistent per layout, and would make
opengwasdb's manifest schema depend on this repository's audit vocabulary. The
decision to exclude a row is authored and reviewed here; enforcing it at the
seam keeps it here.

## Consequences

ADR 0023 says the registry "never materialises a derived builder manifest".
This ADR narrows that blanket statement: the registry now materialises exactly
one derived artifact — a row filter of its own audit table — and passes its
path. It still does not read, rewrite, project, or validate a row of Analysis
data, and it still never inspects a built Store. The seam gained one input
file, not a projection layer.

An excluded row is visible in two places: the bundle, with its reason, and the
manifest's sidecar, recording that it was dropped. The builder sees neither the
row nor the sidecar.

See [store-release-workflow.md](../spec/store-release-workflow.md) and
[release-metadata-schema.md](../release-metadata-schema.md).
