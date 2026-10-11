# HapMap3 GRCh38 Reference Resource test suite

Tests for the maintained HapMap3 GRCh38 variant list and its verifier
(opengwas/opengwasdb-stores#208, epic #214).

## Contracts and invariants covered

1. **Declaration completeness** — `resources/reference-resources/hapmap3-hg38/resource.yaml`
   declares `resource_id`, `kind: variant_list`, `genome_build: GRCh38`, the
   canonical variant-id convention, an external reference-root `location`,
   `row_count`, and the source/ALID checksums; `provenance.json` agrees with it.
2. **Benchmarked byte identity** — the declaration pins the 1,206,903-row list's
   SHA-256; where the reference root is present the in-place list is re-hashed
   and counted, and the test skips (not fails) where it is absent, as in CI.
3. **Canonical-ALID rule** — the verifier mirrors the pinned opengwasdb
   `parse_canonical_alid` round-trip: `chr` prefixes, numeric chromosome aliases,
   lower-case alleles, unsorted allele pairs, non-positive positions and
   non-ACGT alleles are refused; GRCh38 alt contigs are accepted. If the pin
   advances to provide `read_variant_list`, a cross-check asserts the two rules
   agree.
4. **Loud refusals** — missing, malformed, duplicate, non-canonical,
   wrong-length, wrong-byte-count and checksum-mismatched lists all raise with
   the path (and line number for a bad line); `main` exits `2`. Source MD5/SHA,
   rsID row count, source header, and the rsID reconciliation balance are gated
   the same way.

## Running the suite

```sh
pixi run python3 tests/hapmap3-resource/test_hapmap3_resource.py
# or through the repository orchestrator
pixi run test-python
```

The suite is hermetic: it uses tiny in-memory fixtures and never requires the
reference root. The one in-place test self-skips when `/data/opengwasdb/reference/`
is unavailable.
