- **`pixi run site-data` collects the site's data and writes one JSON file per
  source (issue #389, epic #385).** It reads the board (project 1, its epics,
  their sub-issues and blocked-by edges), the Analysis Catalog versions, both
  repositories' Releases, every registered Release Bundle, and the epic records
  and yardstick results under `docs/_epics/` and `docs/_yardstick/`. A source
  that cannot be read is written as `available: false` with its error and never
  stops the others, and an epic record is the YAML file an orchestrated epic
  commits when it finishes.
