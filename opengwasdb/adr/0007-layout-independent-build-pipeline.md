# Use a layout-independent build pipeline

Builders use common ingestion, normalisation, validation, and metadata assembly before dispatching to layout-specific writers. Dense and Ragged writers are responsible for physical array/index encoding, not for reimplementing source parsing or domain rules.

This mirrors the layout-independent query engine and keeps source handling, allele normalisation, scale conversion, sample-size modelling, and release metadata consistent across layouts.

