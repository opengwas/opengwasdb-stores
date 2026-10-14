# Candidate emitter

Tests for `resources/generators/lib/emit.py`, the one emitter every Manifest
Generator uses to write a candidate Release Bundle into the Store Registry
(#248, ADR 0035's *Emit*). They run at its two interfaces: `emit()`, which the
Python generators call, and its command line (`--from <dir> --command ...`),
which the R generators call.

Covered by `tests/emit/test_emit.py`:
- A new candidate lands in `stores/<id>/` with `status: candidate`, the
  generator commands it was given, its sidecars and a generated `summary.yaml`,
  and passes the Release Bundle contract.
- Re-emitting over a candidate replaces it whole, a stale sidecar included.
  This holds when the filesystem cannot exchange directories too. The same
  input re-emits byte-identically.
- Emitting over a bundle whose Release Status is past `candidate`, or over a
  directory that is not a bundle, is refused. The registry stays
  byte-for-byte as it was.
- A candidate failing the contract is refused, and the prior candidate is kept.
- These are refused before any path is built: an invalid Store Release id, a
  sidecar name that is not a plain file name, missing generator commands, and
  a rendered `release.yaml` whose status or command log disagrees with the
  emit.
- A second emit of an id is refused while the first holds its lock.
- A candidate stashed by a swap killed between its two moves is put back
  before anything else happens.
- A Reference-Completed candidate resolves its parent in the real registry, not
  in the staging area. An unregistered parent is refused.
- The command line emits exactly what `emit()` would from the same content,
  whatever YAML style the files were rendered in. A refusal exits non-zero. A
  file a bundle does not hold, or an entry of the wrong kind, is refused by
  name.
- No staged or stashed bundle is left after any success or refusal. Only the
  per-id lock files stay, and git ignores them.

```sh
pixi run test --suite emit
```
