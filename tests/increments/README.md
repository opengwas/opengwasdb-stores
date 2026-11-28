# Increment Bundles

Tests for ADR 0045 (#546): an Increment Bundle, `stores/<id>/increments/<nnnn>/`,
changes a validated Reference-Completed Hybrid's membership under the same Store
id. They run at the public entry points, with small fixture bundles and Artifact
Roots in a temporary directory; nothing reads the real `stores/` or `/data`.

Covered by `tests/increments/test_increments.py`:
- **The contract** (`bundle.check`, and `bundle-check` through
  `manifest_generation.check_bundles`): a candidate increment passes and leaves
  `analyses.tsv` as it was; the increment's columns must be the bundle's; an id
  already in the bundle (a replace or re-admission) is refused; numbering,
  at most one open increment, and stray entries; only a validated
  Reference-Completed Hybrid takes one; drops name admitted Analyses and a
  defect drop needs an erratum; `require_added` must be admitted; sidecar
  headers and ancestry evidence; acceptance bound to the reviewed ids and to
  `content_sha256`, so an edit after acceptance fails; an accepted increment
  carries an attested `source-record.json`; status transitions; a published
  increment carries its `published` block, and its rows are folded as declared.
- **The fold** (`ogstores.increment.fold`): rows appended with `analysis_index`
  continuing, drops' exclusion columns set in `analyses.tsv` and
  `quality_checks.tsv`, a drop row appended to `exclusions.tsv`, every other
  byte kept; the folded derived manifest is the spliced order; a later
  increment may drop an earlier addition.
- **The amendment check** (`ogstores.amendment`): an open increment changes no
  published membership; an undeclared row is refused and points to an
  Increment Bundle; a published increment with both `work/` files rewritten
  passes, a later drop included; an exclusions file the publication did not
  rewrite, or `work/` files whose sha256 no increment recorded, are refused;
  `increment_changes` accepts exactly the declared increment, checks a splice
  report's `added`, `dropped` and `n_analyses_after`, and refuses an undeclared
  change beside it.
- **The emit's increment mode** (`emit.emit_increment`, `emit --increment`):
  writes only `increments/<nnnn>/` as a candidate and leaves the staging area
  clean; refuses a bundle that is not validated, one with an open increment, a
  `require_added` id that is not admitted, and a row whose checksum is a bundle
  row's, writing nothing; the command line emits a rendered increment and a
  drop-only one; `generate_candidate.registered_analysis_ids` is what an
  increment run skips.
- **The attestation writer** (`python3 -m ogstores.source_record`): writes
  `source-record.json` from the records and the owner's attested digests, with
  an explicit-null `reference_panel_sha256`; refuses without `--attested-by`,
  `--variant-reference-sha256` or `--ld-panel-digest`, when the rsID policies
  disagree, when a row's assembly differs, when a build took a reference panel
  without its attested digest, and when the increment is no longer a candidate.

Run alone:

```sh
pixi run -e dev python3 tests/increments/test_increments.py
```
