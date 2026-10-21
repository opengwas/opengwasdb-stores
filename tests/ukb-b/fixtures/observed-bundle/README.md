# Observed-bundle goldens for the UKB-B generator (#312)

`release.yaml`, `build.yaml`, `analyses.tsv` and `validation.yaml` exactly as
the UKB-B generator emitted them from the fixture in
`tests/ukb-b/test_ukb_generate_candidate.py` **before** #312 changed it: the
base tree is commit `9b899b7`, the `dev` merge #312 started from (ADR 0037's
single-id completion not yet emitted by this generator). It is a `dev` commit,
so it survives a squash merge of #312.

They are the pinned bytes for `TestOneBundlePerStore`: with no completion
reference the bundle must not change at all, and with one, everything but the
`complete` block, `completion_state` and `validation.yaml`'s `acceptance` block
must still be these bytes. The suite compares against them instead of running the old code, so it
needs no git history and no network.

Three placeholders stand in for what the code under test does not decide:

| Placeholder | Replaces |
|---|---|
| `{fixture}` | the fixture's temp directory (`source_file`, the recorded command) |
| `{fixture-digest}` | `inventory_sha256`: the inventory records that directory and each source's mtime |
| `{generator-digest}` | `generator.version`: the sha256 of the generator module, which any change to it moves |

Regenerate them from the base tree when the fixture itself changes:

```sh
pixi run -e dev python3 tests/ukb-b/test_ukb_generate_candidate.py --write-goldens
```
