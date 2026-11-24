# `ogstores.backfill_observed` on a fixture registry

Tests for #502's backfill, which writes the Store facts (`store_bytes`,
`has_hm3`, `has_rho`, `n_top_hits`) into the Validation Records registered
before `register` recorded them. Each fixture is a Release Bundle with a
register-shaped `validation.yaml`, a few step records and a small Store of
known file sizes under a temporary Artifact Root, so nothing is built.

1. The facts are filled, and nothing else changes: the parsed records are equal
   once the four keys are dropped, and so is the text once their lines are.
   The keys follow `store_bytes`, in register's order.
2. A second run changes no byte.
3. `--dry-run`, run from the command line, writes nothing in the registry or
   under the Artifact Root, and says what it would write.
4. A real run writes nothing under the Artifact Root.
5. A record already holding the facts has them replaced where they stand.
6. A bundle whose Store is not under the Artifact Root is reported by name as
   missing and left alone. A bundle whose record has no `observed` block, and a
   candidate, are skipped. A Store with no register record reports `has_hm3`
   and `has_rho` as `null`, and a recipe with no Rho step `has_rho` as `null`.
7. A record holding a key twice, a Store fact carrying a comment, and an edit
   that would change another key (a column-0 comment inside `observed`) are
   refused: exit 1, and the file keeps every byte.

    pixi run -e dev python3 tests/backfill-observed/test_backfill_observed.py
