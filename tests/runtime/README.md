# Pre-write runtime guard (stores#218)

`tests/runtime/test_runtime_guard.py` covers
`ogstores.runtime.verify_runtime_for_releases`, the guard the supported release
entry point (`pixi run release`) and the Snakefile `onstart` run before any
artifact is written.

It asserts:

- `pixi.toml` follows opengwasdb's `main`, and `pixi.lock` alone holds the
  commit (#253): every `opengwasdb` reference the lock carries (one per
  environment, per platform, plus the package entry) requests `main` and
  resolves the one commit `ogstores.engine.locked_commit` returns, so one
  stale entry cannot select another runtime for one environment;
- the guard reads that commit from the lock it is given, and refuses a lock
  that does not resolve exactly one;
- a missing executable, a known different revision, and a runtime missing any
  subcommand a scheduled plan names are refused;
- a run that re-runs `validate` over step records another engine of the same
  format wrote is accepted (#410): only a completion checkpoint is held to the
  engine that started it, which `tests/run/` covers;
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
