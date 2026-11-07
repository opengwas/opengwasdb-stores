- **The Analysis Catalog is built for reading over HTTP range requests, and
  search finds an Analysis by its own ID (issue #452, epic #424; ADR 0041,
  which supersedes ADR 0039's search columns; `docs/spec/analysis-catalog.md`,
  "Built for reading over HTTP").**
  `catalog.sqlite` now has 32 KB pages, two covering indexes for the website's
  default listings (`analyses_browse_by_label`,
  `analyses_browse_by_sample_size`), a search index merged into one segment, and
  no free pages. `analysis_search` also covers `analysis_id` and
  `source_analysis_id`, so `GCST90476024` or `"ukb-b-10001"` finds its
  Analysis. opengwas.io no longer needs its own re-indexed copy of each
  version. On version `9df14f8dc3cceabf` at 40 ms per request, that copy took
  a first search from 3.0 s to 0.9 s and a filter change from about 2 s to
  0.2 s. Built from the same rows as version `ca63d1274b70bd46` (38,152 listed
  Analyses), the file grows from 34.6 MB to 42.4 MB: the two covering indexes,
  and the IDs in the search index.
