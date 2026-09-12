# `plan()` golden argv

The primary Phase A test: `plan(bundle).argv` against a golden list, one file
per Store Release. Seven releases times roughly five steps is about thirty-five
assertions, and **none of them need a fixture Store** -- `plan()` opens no
Store and reads no `analyses.tsv` row, so its output is a string.

This is also the review artifact for a change to the seam: a change to
`plan()` shows up as a visible diff in every affected command line at once.

Empty until `src/ogstores/plan.py` is implemented.
