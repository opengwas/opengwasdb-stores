- **This repository's imports of engine modules are bounded by a committed
  baseline, and a new one fails CI (issue #421, epic #416).** Epic #416's
  decision 14: the repositories' seam is wider than documented, and a ratchet is
  the way to stop it widening. `docs/architecture/engine-imports-baseline.yaml`
  lists every engine module this repository imports, one entry per (importing
  file, imported name) — the same unit as the engine's own layering baseline.
  `tests/project-site-architecture/test_engine_imports_baseline.py` fails on an
  import the baseline does not list and on a listed import that has gone, so the
  file only ever shrinks, and a person commits it. It measures with the site's
  architecture collector's import graph, so the Structure and health page's seam
  counts and the baseline cannot disagree, and the page now states the
  baseline's size next to those counts.
