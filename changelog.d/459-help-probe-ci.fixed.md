- **The run-time capability probe reads `--help` however the host draws it (issue #459).** On GitHub
  Actions the engine's typer/rich help forces terminal styling, so `build-ragged-ssf --help` prints
  the option as `ESC[1;36m-ESC[0mESC[1;36m-regionsESC[0m` and a plain match finds nothing: the guard
  then refused every Ragged release carrying a regions table, and the `workflow` suite failed after
  #461 merged. The probe now runs the help with `TERM=dumb`, `NO_COLOR=1`, typer's own
  force-terminal switch and a wide `COLUMNS`, drops the variables that force colour on by being set
  (`FORCE_COLOR`, `PY_COLORS`, `GITHUB_ACTIONS`, `CI`), and strips ANSI escapes before matching — so
  no host setting hides an option the engine provides, and no narrow terminal truncates one.
