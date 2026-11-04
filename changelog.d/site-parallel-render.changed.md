- **The project site renders over 24 Quarto processes, so `pixi run site` and
  `site-publish` take about a minute instead of five (#388).** Quarto renders
  a site's pages one after another, about a second each. `project_site.render`
  gives each worker its own copy of the staged docs, renders one page per
  `quarto render` call and merges the workers' output. A page comes from the
  worker that rendered it, any other file that differs between workers stops
  the render, and the search index and sitemap are rebuilt page by page.
  `--jobs N` sets the worker count, and `--jobs 1` is Quarto rendering alone.
  On 4 Nov 2026 the 226 pages of `main` rendered in 67 s over 24 workers,
  against 319 s alone. 470 of the 471 files the renders share besides the
  search index and sitemap were byte-identical. The other was the registry
  diagram on Structure and health, which Quarto's Graphviz lays out differently
  on every render, of the same source too.
