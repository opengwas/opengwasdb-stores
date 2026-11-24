- **The project site moved to og-dev-hub, with its history (og-dev-hub#7), so
  `pixi run site`, `site-preview`, `site-publish`, `site-data` and
  `architecture` are no longer tasks here.** Run them in og-dev-hub, which reads
  this repository's `stores/`, `docs/`, `docs/_epics/` and `docs/_yardstick/`
  from its clone of `main`, or from a checkout named with `--registry`. The site
  is published at <https://blog.opengwas.io/og-dev-hub/>; the address
  `blog.opengwas.io/opengwasdb-stores/` is retired with no redirect.
  `src/project_site/`, its tests, the Quarto project (`docs/_quarto.yml`,
  `docs/pre-render.sh` and the eight generated section pages), `docs/site.md`
  and `docs/spec/project-site.md` left with it; the `project_site` component is
  gone from `docs/architecture/components.yaml`, and `tools/build-site.sh`
  (`pixi run docs`, the report build) is claimed by `dev_tools`. This
  repository's own architecture checks stay, in `tests/architecture/`, and
  import `project_site` from og-dev-hub, which the `dev` environment installs.
