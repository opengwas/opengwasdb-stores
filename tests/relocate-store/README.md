# `tools/relocate_store.py` on synthetic releases

Tests for #331: moving a published release to another Artifact Root with its
step records. Each release is six records written by `run.record_text` and an
empty Store directory, so nothing is built. An `opengwasdb` stub on PATH
reports whatever Store id a test chooses.

The whole-stack seams belong to the workflow suite (`TestWorkflowEndToEndRelocation`,
contract 15 in `tests/workflow/README.md`). There, a fixture release moved by the
tool has nothing to do at its new root and its next register passes. The same
release moved by a plain rename is refused at that register. The workflow suite
also covers the pending-publication, staged-Store, existing-target and
another-root refusals, and `--dry-run`.

Covered here, each refusal leaving every file under the test's directory
unchanged:
- a run holding the release's lock at the old root, then at the new root;
- a Store whose `opengwasdb info` reports another `store_id`;
- a record that `run.record_text` would not write back byte for byte, with the
  difference;
- the old root left in a record field the tool does not rewrite
  (`replaced_archive`).

A move that fails before its fourth record replacement is also covered,
through the test-only `OGSTORES_TEST_RELOCATION_FAIL_AT`. The tool exits non-zero,
naming the receipt and `--resume`. Three records are as planned and three as
they were, and the receipt says `started`. A further run refuses, naming the
receipt. `--resume` then finishes the move. The records, bytes and mtimes,
equal those of an uninterrupted move of the same release between the same
roots, and so does the receipt, apart from its times.

```sh
pixi run test --suite relocate-store
```
