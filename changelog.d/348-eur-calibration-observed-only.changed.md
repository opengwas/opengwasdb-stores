- **`compare_eur_imputation.py` no longer counts a completed Store's imputed
  cells as truth or as training data (issue #348).** The EUR calibration gate
  now runs against a Reference-Completed Store (OGS-00009), whose `imputed`
  cells are the LD panel's own substitutions: counting one as a held-out beta
  measured the panel against itself. Truth and training are restricted to cells
  whose `imputed` flag is 0, and a Store with no `imputed` plane is read with
  every cell observed, exactly as before. The script also decodes the `z`, `se`
  and `imputed` planes through the core's decoders instead of reading the raw
  arrays -- a format-0.2.0 `se` plane is int8 residuals against the cell's
  frequency, so the raw read used to feed codes where standard errors belong --
  and its JSON sidecar records the Store's format version, completion state,
  encoding and the observed-only rule it applied.
