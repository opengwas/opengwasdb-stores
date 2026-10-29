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
# Rendering that mirror with Quarto by hand needs this file to have run first,
# which is what the hook is for; `python3 -m project_site.pages --docs docs`
# writes the same pages into the checkout, where they are git-ignored.
set -eu
exec python3 -m project_site.pages --docs .
