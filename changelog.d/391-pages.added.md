- **The site now shows where the project stands: an Overview, a page per epic,
  a page per Store, trends and a changelog (issue #391, epic #385).** They are
  generated at render time from the collected data by
  `src/project_site/pages.py`: milestone progress, each workstream's active
  epics and their next step, the headline figures with a trend line, every
  epic's tickets as a Mermaid graph, and each Store's lifecycle, counts, catalog
  decision and release notes. Every number traces to one collected field, and a
  total over a column any Store leaves `null` reads `unknown (k of n Stores
  report it)` rather than a partial sum.
