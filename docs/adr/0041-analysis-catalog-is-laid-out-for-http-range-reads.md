# The Analysis Catalog is laid out for reading over HTTP, and opengwas.io's browse indexes are part of its contract

opengwas.io's Data page reads `catalog.sqlite` in the browser over HTTP range
requests (opengwasdb-website decision 13). Each request is a round trip, so the
file's layout sets how fast the page is. Until #452 the website re-indexed its
own copy of each version to make it fast. So the file it published was not the
catalog's, and its checksum differed. ADR 0039 left the website out of scope.
This records what the catalog now promises it.

## Decision

**The builder writes `catalog.sqlite` for range reads.**

- **32 KB pages,** set before the first table. The website asks for one page per
  request, so a larger page means fewer trips.
- **Two covering partial indexes** (`WHERE listed = 1`), one for each of the Data
  page's default listings:
  - `analyses_browse_by_label`, in `analysis_label, id` order;
  - `analyses_browse_by_sample_size`, in `sample_size DESC, id DESC` order.

  Each holds every column of the page's results table (`RESULTS_COLUMNS`), so a
  page of results is read from the index alone.
- **One search segment.** FTS5 `optimize` runs after `rebuild`.
- **No free pages.** If that merge leaves any, the database is vacuumed once
  after it is committed.

**Search covers each Analysis's own IDs**, `analysis_id` and
`source_analysis_id`, as well as the label, ontology label, consortium and first
author. This supersedes ADR 0039's list of what the full-text index covers.

**The two index names and the results table's columns are a contract with
opengwasdb-website.**

- The site looks for the indexes by name, uses them with `INDEXED BY`, and
  falls back to the one-column indexes when they are missing. The fallback is
  slower, but not wrong.
- Renaming either index, or dropping a column the results table shows, needs
  the website changed first.
- A new column in the website's results table needs `RESULTS_COLUMNS` changed
  here.

## Consequences

- **Size and build time.** Measured on the same rows as version
  `ca63d1274b70bd46` (38,152 listed Analyses):
  - the file grows from 34.6 MB to 42.4 MB, from the two indexes and the IDs in
    the search index;
  - `pixi run catalog`, including the summary render, takes 17.5 s.
- **Speed.** Measured on `9df14f8dc3cceabf` at 40 ms per request (#424), the
  website's equivalent copy took:
  - a first search from 3.0 s to 0.9 s;
  - a filter change from about 2 s to 0.2 s.
- **The website's copy goes.** The website can drop `make_browse_catalog.py` and
  publish the catalog's own file, whose checksum then matches.
- **Not measured at scale.** `VACUUM` rewrites the file once more. Its cost at
  the size of ADR 0039's 5M-row prototype (2.3 GB) has not been measured.
