# Use a hybrid SQLite, tabix, and Zarr store envelope

Each Store Release uses a small root manifest, an embedded SQLite database for
compact relational metadata, a tabix-indexed variant table for the
high-cardinality variant axis, and a Zarr hierarchy for numerical association
arrays. This keeps OpenGWASDB stores standalone while using SQLite for
low-cardinality metadata, tabix for genomic lookup, and Zarr for compressed
chunked numerical payloads.

The standard release layout is:

```text
manifest.json
index.sqlite
data.zarr/
variants.tsv.gz
variants.tsv.gz.tbi
variant_offsets.npy
```
