#!/bin/sh
# Generate the site's data pages into the tree Quarto is about to render (#391).
#
# `docs/_quarto.yml` names this file under `project.pre-render`, and Quarto runs
# it with the working directory set to the project — the docs mirror that
# `pixi run site` and `pixi run site-publish` stage under
# /data/opengwasdb/work/site (src/project_site/build.py). So the pages are
# written where Quarto reads them and never into the checkout: `--docs .` is the
# mirror, and `python3` is the pixi environment's, which has `project_site`
# installed.
#
# A hand run needs `--docs` naming a tree to write, and it replaces the five
# section pages the checkout commits as short "this page is generated" stubs, so
# point it at the mirror (`--docs /data/opengwasdb/work/site/<checkout>/_stage`)
# rather than at `docs/`: only the per-epic and per-Store pages are git-ignored.
set -eu
exec python3 -m project_site.pages --docs .
