# Separate release manifests from build recipes

Release Manifests record the exact accepted inputs for a Store Release, while Build Recipes record how those inputs are transformed into an OpenGWASDB store. Keeping them separate allows the same inputs to be rebuilt or benchmarked with different layout, indexing, normalisation, or validation choices without changing the source membership record.
