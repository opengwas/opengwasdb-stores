# Locus Lead report extract (stores#444)

`tests/locus-leads-extract/test_locus_leads_extract.py` runs
`tools/locus_leads_prototype_extract.py` on small synthetic outputs shaped like
the engine's prototype outputs, never the engine's own files: an engine
stand-in serves them where the extract would run `git show`.

Covered, each test seen to fail first (against the code before the check
existed, or against a named mutation):
- a later ticket's copy of #403's numbers that differs in any field is refused,
  naming the path and both values: #409's `set_beside[0].median_ratio` raised
  by one, and a per-lead count deep in `by_abs_z`; the unchanged fixture passes,
  and #409's own C3 reading is not taken for a copy;
- the sha256 recorded for an output is of the bytes served (`{}`, worked by
  hand), and #404's maps naming another sweep than the committed one are
  refused;
- an output missing a key refuses, naming the key, instead of building a
  partial extract;
- two derivations against hand-worked values: how many disagreeing pairs lie
  past 1 Mb or reach r² 0.05, and the best |log2| gain over bp-only at the same W.

Run from the repository root:

```sh
python3 tests/locus-leads-extract/test_locus_leads_extract.py
```
