# Source Inventory test suite

Test suite for the Phase B frozen Source Inventory seam (issue #151), owned by
[`resources/generators/lib/source_inventory.py`](../../resources/generators/lib/source_inventory.py)
and driven by
[`resources/generators/gwas-catalog-eur-hybrid/inventory.py`](../../resources/generators/gwas-catalog-eur-hybrid/inventory.py).
See [`resources/inventories/README.md`](../../resources/inventories/README.md)
for the column contract and the readiness vocabulary.

## Contracts and invariants covered

1. **Retry-overlay precedence** — the retry pass is authoritative for every
   `analysis_id` it covers, in both directions: it turns a failed transfer into a
   ready source, and it can also record that a previously accepted file's header
   no longer passes. Both manifests' per-status counts and the override count
   survive into the provenance sidecar, so a count that differs from the base
   manifest's is explained rather than silently replaced.

2. **The readiness vocabulary is explicit** — `ok` and `already_present` are
   ready; `missing_remote_harmonised_yaml`, `header_rejected`,
   `metadata_rejected`, `data_failed`, `yaml_failed`, `dry_run` and `error` are
   not. Every status in the fixture inventory is classified, an unknown status
   fails loudly, and unavailable inputs stay inventory rows instead of being
   presented as members.

3. **Accounting** — a duplicate `analysis_id` inside a manifest, a duplicate
   candidate accession, a candidate with no acquisition row, an acquisition row
   outside the candidate pool, and a missing required manifest column all fail
   with the offending identity named.

4. **Determinism** — two freezes of the same inputs produce identical inventory
   bytes, and per-run download timing (`seconds`) never reaches the frozen file.

5. **Exact source paths** — both harmonised filename shapes
   (`<GCST>.h.tsv.gz` and `<PMID>-<GCST>-<EFO>.h.tsv.gz`) are recorded exactly as
   acquisition wrote them; nothing reconstructs a name from an accession.

6. **Duplicate content** — two ready accessions with one checksum are reported as
   a group and both stay in membership; collapsing them is a human decision.

7. **Preflight passes on a healthy snapshot** and reports the plan: per-status
   counts, ready count/bytes and design split, the method tier per study design,
   every expected exclusion, duplicate groups, each declared Reference Resource
   with presence/size/required, the requested cores, and the work root's
   usability and free space.

8. **Preflight fails loudly** on a missing or resized ready source file, missing,
   unreadable or mis-declared metadata, an inventory edited after freezing, a
   missing provenance sidecar, a snapshot that is not the one the config
   declares, a study design with no declared method tier, an absent required
   Reference Resource (including an ancestry-mixture reference's fine-group map),
   a Reference Resource referenced but not declared, an over-cap or non-positive
   core count, an unusable work root, and free space below the declared minimum.

9. **No association body is ever read** — a tripwire fails the test if preflight
   opens any `.h.tsv.gz`/`.bgz` file, through `open`, `io.open` or `gzip.open`.

10. **The shipped artifacts stay consistent** — `config-full.yaml`,
    `resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-10.tsv` and its
    provenance sidecar agree on the snapshot id, the inventory checksum, the
    issued readiness baseline (6,035 rows / 4,570 ready / 1,766,662,881,302
    bytes), the two duplicate-content pairs, and both method tiers; and every
    `resource.yaml` in this repository still loads.

## Running the suite

```sh
pixi run --environment dev python3 tests/source-inventory/test_source_inventory.py
# or through the repo orchestrator
pixi run test-python
```

The suite is fixture-scale and hermetic: it builds its own mirror, candidate
table, acquisition manifests and config in a temporary directory, and never
reads `/data`, the network, or the acquisition mirror. The one non-fixture test
reads only tracked files.
