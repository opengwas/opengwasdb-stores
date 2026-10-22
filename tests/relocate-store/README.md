# `tools/relocate_store.py` on synthetic releases

Tests for #331: moving a published release to another Artifact Root with its
step records. Each release is six records written by `run.record_text` and an
empty Store directory, so nothing is built. Two of the records have mode
0600. An `opengwasdb` stub on PATH reports whatever Store id a test chooses.

The whole-stack seams belong to the workflow suite (`TestWorkflowEndToEndRelocation`,
contract 15 in `tests/workflow/README.md`). There, a fixture release moved by the
tool has nothing to do at its new root and its next register passes. The same
release moved by a plain rename is refused at that register. The workflow suite
also covers the pending-publication, staged-Store, existing-target and
another-root refusals, and `--dry-run`.

A refusal exits 2, and every one here leaves the test's directory unchanged.
The roots' `.release-locks/` files don't count, because taking a lock leaves its
file, as `pixi run release` does. Covered here:
- a run holding the release's lock at the old root, then at the new root;
- a Store whose `opengwasdb info` reports another `store_id`;
- a record that `run.record_text` would not write back byte for byte, with the
  difference;
- the old root left in a record field the tool does not rewrite
  (`replaced_archive`);
- a test fault value that names no replacement;
- a receipt that cannot be written. The tool removes what it wrote of the
  receipt and the `relocations/` directory it made, and says nothing was moved
  or written.

A move made to fail before its fourth record replacement, through the test-only
`OGSTORES_TEST_RELOCATION_FAIL_AT`, behaves like this:
- The tool exits 1, naming the receipt and `--resume`, and does not say nothing
  was moved.
- Three records are as planned and three as they were, and the receipt says
  `started`.
- A further run refuses, naming the receipt and `--resume`.
- `--resume` then finishes the move. The records match those of an
  uninterrupted move of the same release between the same roots: bytes, modes
  (0600 kept) and mtimes. So does the receipt, apart from its times.

Each `--resume` refusal changes nothing and does not send the operator back to
`--resume`. The cases are:
- a record at neither its hash before nor the one planned;
- a receipt for another move;
- both directories existing;
- two unfinished receipts;
- a record added after the failure;
- a record deleted after the failure;
- a pending publication;
- a staged Store;
- no unfinished move.

```sh
pixi run test --suite relocate-store
```
