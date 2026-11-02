- **The Work map places tickets that cite package-relative paths (issue #416).**
  A ticket citing `encoding/codec.py` or `layouts/ragged/zarr_csr.py:551` now lands
  on the component that owns `opengwasdb/encoding/codec.py`, when exactly one tracked
  file ends that way; an ambiguous citation is listed as unresolved instead of
  guessed. Tickets and ADRs share one resolver.
