#!/usr/bin/env bash
#
# Render the reports and assemble the docs/ site that GitHub Pages serves
# (Settings -> Pages -> Source: main branch, /docs folder).
#
# Requires `quarto` and `Rscript` on PATH; run via the repository's Pixi
# `docs` (or `dev`) environment (issue #41):
#   pixi run --environment docs docs
#
# docs/index.html and docs/.nojekyll are hand-maintained and not regenerated
# here. The imputation-filters report needs a ~2 GB summary-statistics download
# plus its knitr cache, so it is only refreshed when a local render already
# exists; render it explicitly with:
#   pixi run quarto render resources/scripts/mvp-imputation-filters.qmd
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
DOCS="$ROOT/docs"
mkdir -p "$DOCS"

echo "==> store-curation report"
quarto render resources/scripts/ebi-studies.qmd --to html

echo "==> beta-scale estimation report"
quarto render resources/scripts/beta-scale-estimation.qmd --to html

echo "==> query walkthrough"
quarto render resources/scripts/query-walkthrough.qmd --to html

echo "==> canonical trait mapping report (reads the committed docs/data/trait-mapping-* exports)"
quarto render resources/scripts/trait-mapping-report.qmd --to html

echo "==> prioritisation dashboard"
Rscript resources/scripts/make-dashboard.r

echo "==> assembling docs/"
install_html() { cp -f "$1" "$2" && echo "    $(basename "$2")"; }
install_html resources/scripts/ebi-studies.html                         "$DOCS/store-curation.html"
install_html resources/scripts/beta-scale-estimation.html               "$DOCS/beta-scale-estimation.html"
install_html resources/scripts/query-walkthrough.html                   "$DOCS/query-walkthrough.html"
install_html resources/scripts/trait-mapping-report.html                 "$DOCS/trait-mapping-report.html"
install_html resources/data/derived/store-prioritisation-dashboard.html "$DOCS/prioritisation-dashboard.html"
if [ -f resources/scripts/mvp-imputation-filters.html ]; then
  install_html resources/scripts/mvp-imputation-filters.html            "$DOCS/imputation-filters.html"
else
  echo "    (skipped imputation-filters.html — render it first to refresh)"
fi

echo "Done. Commit docs/ and push to update GitHub Pages."
