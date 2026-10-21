#!/usr/bin/env bash
#
# Render the reports and assemble the docs/ site that GitHub Pages serves
# (Settings -> Pages -> Source: main branch, /docs folder).
#
# Requires `quarto` and `Rscript` on PATH; run via the repository's Pixi
# `docs` (or `dev`) environment (issue #41):
#   pixi run --environment docs docs
#
# The report sources live in docs/reports/; each renders beside its source
# (gitignored) and is copied to the docs/ name the site publishes, so links to
# the published pages do not change when a source moves (ADR 0038).
#
# docs/index.html and docs/.nojekyll are hand-maintained and not regenerated
# here. The imputation-filters report needs a ~2 GB summary-statistics download
# plus its knitr cache, so it is only refreshed when a local render already
# exists; render it explicitly with:
#   pixi run quarto render docs/reports/mvp-imputation-filters.qmd
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DOCS="$ROOT/docs"
mkdir -p "$DOCS"

echo "==> store-curation report"
quarto render docs/reports/ebi-studies.qmd --to html

echo "==> beta-scale estimation report"
quarto render docs/reports/beta-scale-estimation.qmd --to html

echo "==> query walkthrough"
quarto render docs/reports/query-walkthrough.qmd --to html

echo "==> canonical trait mapping report (reads the committed docs/data/trait-mapping-* exports)"
quarto render docs/reports/trait-mapping-report.qmd --to html

echo "==> prioritisation dashboard"
Rscript docs/reports/make-dashboard.r

echo "==> assembling docs/"
install_html() { cp -f "$1" "$2" && echo "    $(basename "$2")"; }
install_html docs/reports/ebi-studies.html                              "$DOCS/store-curation.html"
install_html docs/reports/beta-scale-estimation.html                    "$DOCS/beta-scale-estimation.html"
install_html docs/reports/query-walkthrough.html                        "$DOCS/query-walkthrough.html"
install_html docs/reports/trait-mapping-report.html                     "$DOCS/trait-mapping-report.html"
install_html resources/data/derived/store-prioritisation-dashboard.html "$DOCS/prioritisation-dashboard.html"
if [ -f docs/reports/mvp-imputation-filters.html ]; then
  install_html docs/reports/mvp-imputation-filters.html                 "$DOCS/imputation-filters.html"
else
  echo "    (skipped imputation-filters.html — render it first to refresh)"
fi

echo "Done. Commit docs/ and push to update GitHub Pages."
