# Locus Lead report extract (stores#444)

`tests/locus-leads-extract/test_locus_leads_extract.py` runs
`tools/locus_leads_prototype_extract.py` on small synthetic outputs shaped like
the engine's prototype outputs, never the engine's own files: an engine
stand-in serves them where the extract would run `git show`, and a panel
stand-in serves variant-table headers where it would read and decompress the
panel's files.

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
- the per-Analysis counts behind the report's count figure, on three
  hand-worked Analyses: index variants that differ between #403's sweep and
  #409's profile are refused, naming the Analysis and both values, and so is a
  profile that misses an Analysis of the sweep; a committed ρ or median ratio
  that the per-Analysis counts do not give back is refused, naming the rule,
  the statistic and both values; a count of 0, which a log axis cannot show, is
  refused; and so is a bp-only count above the ceiling;
- the bp-only ceiling: worked by hand from two chromosome lengths, with each
  variant table's sha256 recorded; a table on another assembly, or describing
  another chromosome, is refused;
- the region figures' data (#409's `eur_regions.json`): a file the engine
  commit does not hold is refused, naming it; so is one built on another sweep
  than the committed one, or on a panel table hashed differently from the one
  the ceiling read; an Analysis whose whole-genome index variants, in its
  record or in its list of positions, differ from the per-Analysis records is
  refused, naming both values; so is a rule with more leads in the view than
  in the whole genome; and one region's counts are worked by hand (rows drawn
  and not, index variants, leads, splits and lost, and the rows' TSV);
- derivations against hand-worked values: how many disagreeing pairs lie past
  1 Mb or reach r² 0.05, the best |log2| gain over bp-only at the same W,
  Spearman's ρ with tied ranks (√0.9), and the Analyses with more clumps than
  the ceiling.

Run from the repository root:

```sh
python3 tests/locus-leads-extract/test_locus_leads_extract.py
```
