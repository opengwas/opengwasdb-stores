- **The Work map places tickets that cite package-relative paths (issue #416).**
  A ticket citing `encoding/codec.py` or `layouts/ragged/zarr_csr.py:551` now lands
  on the component that owns `opengwasdb/encoding/codec.py`, when exactly one tracked
  file ends that way; an ambiguous citation is listed as unresolved instead of
  guessed. Tickets and ADRs share one resolver.
- **The Work map marks a cited path that does not exist (issue #416).** A ticket
  that cites a file no tracked file matches still shows its work on the component
  that claims the path, but the signal says `not found` and the Work page lists it,
  so a typo is visible.
