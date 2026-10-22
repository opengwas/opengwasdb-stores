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
  or written;
- a receipt that cannot be written on a move straight back, within the same
  second. The first move's receipt beside it stays.

A move made to fail through the test-only `OGSTORES_TEST_RELOCATION_FAIL_AT` is
finished by `--resume`. It is failed at three points:
- before the rename: no record rewritten, and the receipt still at the old
  root;
- before the fourth record replacement: three records rewritten;
- before the receipt is complete: all six rewritten.

For each:
- The tool exits 1, naming the receipt and the exact `--resume` command, and
  does not say nothing was moved.
- The receipt says `started`, and the expected number of records are as
  planned.
- A further run refuses, naming the receipt and that command.
- A file that a failed replacement would leave beside a record is planted.
  `--resume` finishes the move and removes that file. The records match those
  of an uninterrupted move of the same release between the same roots: bytes,
  modes (0600 kept) and mtimes. So does the receipt, apart from its times.

Run from where the release now is, a plain run names the `--resume` with the
receipt's own roots. Once a record has changed since the failure, it names the
reasons `--resume` would refuse instead, and not `--resume`.

Each `--resume` refusal changes nothing and does not send the operator back to
`--resume`. None leaves a lock file under a root the move does not name. The
cases are:
- a record at neither its hash before nor the one planned;
- a receipt for another move;
- both directories existing;
- two unfinished receipts;
- a record added after the failure;
- a record deleted after the failure;
- a pending publication;
- a staged Store;
- a record that cannot be read;
- a receipt with no list of resumes;
- a mistyped root;
- no unfinished move.

```sh
pixi run test --suite relocate-store
```
