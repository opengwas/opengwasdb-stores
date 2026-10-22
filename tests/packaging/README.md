# Packaging and suite discovery

Tests for #257 and #249: the repository's Python code installs editable into
the pixi environments (`pyproject.toml`), the test runner finds the suites
itself, and the two workflows meet only at the Release Bundle.

Covered:
- `test_discovery.py` checks `tests/run_all_tests.py`:
  - a Python suite is named after its directory, or `<directory>/<file>`
    when the directory holds several;
  - a name an R suite already has falls back to the file form;
  - a split suite runs as its named parts, and in this repository every
    test of a split file runs in exactly one part;
  - a suite in `SHARDED_SUITES` runs as up to one process per job, its
    tests cut in listed order, and one that selects no test is refused;
  - in this repository every Python test file is exactly one suite, and the
    suite names CI uses are kept.
- `test_discovery.py` also checks that two suites sharing a name are
  refused, that this environment's `ogstores` is this checkout's, and that
  CI's release-stack suites keep their names and order.
- `test_imports.py`: no tracked Python file, and not the Snakefile, inserts
  the repository root or `src/` into `sys.path`. That includes inserts
  through a loop over a literal tuple, and `src/` handed to a subprocess
  through `PYTHONPATH`. Manifest Generation code (`src/manifest_generation`,
  `resources/generators`) and the tests go further (#249): they insert
  nothing into `sys.path`, not even a script's own directory, and load no
  module by file path (`spec_from_file_location`, `SourceFileLoader`,
  `runpy.run_path`). The few tests of scripts that are not generation code
  and cannot be imported are listed, each with its reason, in
  `EXEMPT_TESTS`, and an exemption that is no longer needed fails.
- `test_boundary.py` (#249, ADR 0035): Store Release Build (`src/ogstores`,
  `workflow/Snakefile`, `workflow/release.py`) neither imports Manifest
  Generation (`manifest_generation`, `resources`) nor runs it by module or
  path, and the Snakefile `include:`s neither `generate.smk` nor anything
  under `manifest_generation` or `resources/generators`. `manifest_generation`
  is installed from this checkout, and imports from `ogstores` only
  `CONTRACT_AND_SHARED_LAYOUT`: the Release Bundle contract (`bundle`) and the
  registry layout it shares (`paths`, `index`). Each scanner has a positive
  control.
- `test_per_source_statistics.py` (#287, ADR 0036): no module under
  `src/manifest_generation/providers/` -- per-Source-Provider connector code --
  computes ancestry, allele alignment or phenotype SD, or drives that
  computation itself. The AST scan refuses imports of OpenGWASDB's ancestry,
  phenotype-SD, allele-orientation and resolution internals and of the AF
  extraction they read through, imports of this repository's helpers that do
  it, and string routes around the import check (a `python3 -m` module path,
  `importlib.import_module`, or the `assign-ancestry`,
  `estimate-phenotype-sd` and `resolve-analyses` subcommands); docstrings are
  not scanned. Positive and negative controls pin each route. Every source's
  connectors and release config are on the pipeline (epic #282 stage 2), so no
  provider module is exempt: the scan covers all of them. The code's migration
  is not a collection's: FinnGen's (`OGS-00017`) and ukb-b's (`OGS-00009`) full
  comparison runs are still pending.
- `test_layout.py` (#317, ADR 0038): one home per kind of thing. No `.py`
  file lives outside `src/`, `tests/`, `tools/` and `workflow/`, which
  catches a root module or a namespace package such as `rogue/module.py`, and
  only `src/` and `tests/` hold packages. Nothing under `resources/` is code
  (by suffix, a Snakefile, or a shebang line) except two R metadata resolvers
  curation's R-resolver round-trip still sources, each listed in
  `LEGACY_R_GENERATORS` with the ticket that retires it; a listed file that is
  gone fails, so the list only shrinks.
  `resources/scripts/` stays gone. A positive control pins each scanner.
- `test_tool_paths.py` (#317): every moved script that derives the repository
  root from `__file__` resolves its defaults inside this checkout. It runs
  `check_allele_orientation.py --plan-only`, the site build with stand-in
  renderers, and otherwise the defaults each tool computes when loaded.
- `test_packages.py` (#249): `pyproject.toml`'s package list equals the
  directories under `src/manifest_generation` that hold an `__init__.py`, and
  every directory of Python there is a package, except `pilot/guard` (a
  `sitecustomize.py` copied onto a resolver child's `PYTHONPATH`).

```sh
pixi run test --suite packaging/discovery --suite packaging/imports --suite packaging/boundary \
  --suite packaging/packages --suite packaging/layout --suite packaging/tool-paths
```
