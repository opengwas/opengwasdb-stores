- **One Quarto site is served from `gh-pages` over both repositories' docs,
  and `pixi run site` renders it on the host where the data lives (issue #388,
  epic #385).** The render stages a mirror of `docs/`, the engine's docs at the
  revision `pixi.lock` pins and the glossary, rewrites the links that leave
  `docs/` to GitHub at the commit being rendered, and checks that every path the
  legacy Pages deployment serves is still there, so no published link breaks.
  `pixi run site-publish` renders a clean checkout of the ref and commits the
  output to `gh-pages`; nothing is rendered in CI.
