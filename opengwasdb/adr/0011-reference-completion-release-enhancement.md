# Make reference completion an optional release enhancement

Ingestion first builds an Observed-Only Store Release that is immediately queryable. Reference completion is an independent optional phase that can produce a Reference-Completed Store Release by adding imputed associations against a declared Reference Variant Set while preserving the ability to distinguish observed from imputed associations.

Published Store Releases remain immutable: enhancement produces a new release rather than mutating a published one. The enhanced Reference-Completed release keeps the same Store identity as the Observed-Only source release and receives a new release identity.

Reference completion always produces the full canonical statistic pair, Z and SE, for imputed associations. There is no Z-only completion mode. The LD Reference Panel defines the Reference Variant Set for the completed release, and the Reference Completion Method is recorded once as release-level provenance. Reference Completion Quality is recorded at LD-block-by-Analysis granularity rather than per association.

