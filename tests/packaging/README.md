# Packaging and suite discovery

Tests for #257 and #249: the repository's Python code installs editable into
the pixi environments (`pyproject.toml`), the test runner finds the suites
itself, and the two workflows meet only at the Release Bundle.

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
  through `PYTHONPATH`. Manifest Generation code (`src/manifest_generation`,
  `resources/generators`) and the tests go further (#249): they insert
  nothing into `sys.path`, not even a script's own directory, and load no
  module by file path (`spec_from_file_location`, `SourceFileLoader`,
  `runpy.run_path`). The few tests of scripts that are not generation code
  and cannot be imported are listed, with the reason, in `NOT_GENERATION`.
- `test_boundary.py` (#249, ADR 0035): Store Release Build (`src/ogstores`,
  `workflow/Snakefile`, `workflow/release.py`) neither imports Manifest
  Generation (`manifest_generation`, `resources.generators`) nor runs it by
  module or path; `manifest_generation` is installed from this checkout; and
  Manifest Generation imports from `ogstores` only the Release Bundle
  contract modules (`bundle`, `paths`, `index`).

```sh
pixi run test --suite packaging/discovery --suite packaging/imports --suite packaging/boundary
```
