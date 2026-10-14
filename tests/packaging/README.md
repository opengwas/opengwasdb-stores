# Packaging and suite discovery

Tests for #257: the repository's Python code installs editable into the pixi
environments (`pyproject.toml`), and the test runner finds the suites itself.

Covered:
- `test_discovery.py` checks `resources/scripts/run_all_tests.py`:
  - a Python suite is named after its directory, or `<directory>/<file>`
    when the directory holds several;
  - a name an R suite already has falls back to the file form;
  - a split suite runs as its named halves;
  - in this repository every Python test file is exactly one suite, and the
    suite names CI uses are kept.
- `test_discovery.py` also checks that two suites sharing a name are
  refused, that this environment's `ogstores` is this checkout's, and that
  CI's exclusive suites keep their names and order.
- `test_imports.py`: no tracked Python file, and not the Snakefile, inserts
  the repository root or `src/` into `sys.path`. That includes inserts
  through a loop over a literal tuple, and `src/` handed to a subprocess
  through `PYTHONPATH`. Inserting a script's own directory is #249's to
  retire.

```sh
pixi run test --suite packaging/discovery --suite packaging/imports
```
