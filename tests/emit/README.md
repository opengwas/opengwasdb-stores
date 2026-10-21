# Candidate emitter

Tests for `src/manifest_generation/emit.py`, the one emitter every Manifest
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
- A legacy generator's working bundle projects to the current contract:
  - the release and build documents come from its config: the default hm3
    subset on a Dense release, none on Ragged, and an empty list plus the
    written reason for a reviewed opt-out;
  - `analyses.tsv`, flat sidecars and `validation.yaml` come from the working
    bundle;
  - the working bundle's own command log comes first, followed by the
    commands given; its family-shaped documents are not carried over.

  Nothing is guessed: a config missing its `build:` block, layout, `post`,
  label or access posture is refused. At a pinned `created_at`, a re-emit is
  byte-identical.
- `python3 -m manifest_generation.emit work-root <id>` prints `<artifact-root>/<id>/generate`, honouring
  `--artifact-root` over `OPENGWASDB_ARTIFACT_ROOT`; an invalid id has none.
- No generator config names a `families/` path or a `release_dir`, and every
  legacy config names the Store Release id it emits.

```sh
pixi run test --suite emit
```
