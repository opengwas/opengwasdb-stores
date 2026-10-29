- **`pixi run site-publish` checks a render against a fixed legacy baseline,
  not the sources `main`'s `docs/` happens to hold (issue #397).** Every publish
  since the cutover was refused over 23 paths the legacy site never served
  (`_epics/*.yaml`, `_yardstick/*.json`, `_quarto.yml`, `pre-render.sh`, the
  page sources). The baseline is now `resources/site/legacy-served-paths.txt`:
  the 90 paths under `docs/` at `ff204b6`, the commit the live legacy Pages
  deployment was built from, so the links readers hold stay guarded while
  generated pages come and go. A missing, empty or malformed baseline fails the
  check rather than passing an unchecked render.
