# Store id reorder migration tests

Regression tests for the owner-authorized renumbering (2026-10-13):

- FinnGen is `OGS-00017` (migrated from `OGS-00016`) with a `migration` block and
  a Release Erratum; the old directory no longer exists and no other bundle
  claims the `r13-full` label.
- The Build Recipe carries the new `store_id`; the `by-label/r13-full` symlink
  points at `../OGS-00017`; the membership table is still 2,754 Analyses.
- The MVP config/tracked definitions allocate META to `OGS-00016` and FinnGen to
  `OGS-00017`.
- ADR 0022 carries the owner-authorized-renumbering amendment, and the migration
  evidence doc records the byte-identical artifact hashes.

```sh
python3 tests/migration/test_id_reorder.py
```
