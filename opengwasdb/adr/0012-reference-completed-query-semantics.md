# Include imputed associations by default in reference-completed queries

Queries against Reference-Completed Store Releases include imputed associations by default because the purpose of reference completion is to avoid missingness and replace slow query-time LD proxy lookup. Returned results must expose Association Status so callers can distinguish observed, imputed, and missing values, and query APIs provide an observed-only mode for analyses that require source-observed associations only.

For exact and range queries against Dense Reference-Completed releases with Ragged Overflow, overflow results are included by default so observed off-panel associations do not silently disappear. Top-hit queries give Dense, Ragged, Ragged Overflow, observed, and imputed associations the same priority; results are merged and ranked by significance rather than storage component.

Detailed Reference Completion Quality is not joined onto every returned imputed association by default. Query APIs may expose it when requested, while default result rows keep Association Status and Query Component as the minimal provenance fields.

