The architecture map's `conflicts` no longer says "meets no other work" about
code it cannot place, or about data a fetch failed to update (epic #416). A
code path that no component claims (a mistyped `opengwasdb/cli.py`, or a new
module whose claim does not exist yet) used to count as touching nothing, so
the orchestrate check launched the lane; it now exits 2, naming the path. A
fetch recorded as failed (`--refresh` offline, or the collection's own) used to
be ignored, so stale branches answered as current; `conflicts` and `check` now
exit 2, naming what could not be fetched, unless they find a problem anyway.
Tests and docs given with `--files` are listed as outside the map. On 3 Nov,
`conflicts --refresh --repo engine --files opengwasdb/cli.py` with every fetch
blocked went from exit 0 ("1 files on 0 components", "meets no other work") to
exit 2, naming the path and the three failed fetches.
