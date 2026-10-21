# Pre-write runtime guard (stores#218)

`tests/runtime/test_runtime_guard.py` covers
`ogstores.runtime.verify_runtime_for_releases`, the guard the supported release
entry point (`pixi run release`) and the Snakefile `onstart` run before any
artifact is written.

It asserts:

- the pin is one immutable revision in `pixi.toml`, `pixi.lock` and
  `ogstores.runtime.PINNED_OPENGWASDB_REVISION`;
- a missing executable, a known different revision, and a runtime missing any
  subcommand a scheduled plan names are refused;
- a scheduled Reference-Completed release whose existing parent Store is format
  0.1.0 is refused, while a 0.2.0 parent or a parent that will be built in the
  same run passes;
- required commands come from the plan, not a hardcoded list;
- a release that completes its own Store is refused, before anything is
  written, on a runtime without `check-observed-view` (#306), naming the
  command and the pin; a plan with no completion phase is not gated on it.

Fixtures are a shell `opengwasdb` stub plus a `direct_url.json`, so the suite
needs neither a real opengwasdb install nor a Store. `pixi run test` discovers
and runs it, as suite `runtime`.

The guard is described in [docs/runtime.md](../../docs/runtime.md) and the
workflow specification's "The pinned current-format runtime (stores#218)".
