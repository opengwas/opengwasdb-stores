# Dense Reference-Completed variant axis is source ∪ reference panel, not panel-only

Supersedes ADR 0015.

For Dense Reference-Completed releases the dense variant axis is the union of source-observed variants and Reference Variant Set variants, sorted by (chr, pos, A1, A2). Source variants that are absent from the Reference Variant Set remain in the dense matrix as observed or missing associations and are never imputed. Reference Variant Set variants not present in the source get new rows; their associations are imputed where the LD block quality gate passes and marked missing where it does not.

ADR 0015 required that the dense axis contain *only* Reference Variant Set variants, and that off-panel source associations be stored in a Ragged Overflow component. The stated benefit was axis-compatibility: two stores completed against the same LD Reference Panel would share an identical variant axis. That property was never actually needed — no use case has required cross-store axis equality for dense stores — and the cost was substantial: a mandatory overflow component, query-time merging of dense and overflow results, and a more complex validation and query layer. Removing Ragged Overflow simplifies every part of the system: the output is a single dense matrix with a single query path, and off-panel source variants contribute their observed associations without any special handling.

Source-only variants cannot be imputed because the LD panel provides no LD structure for them. Their cells remain NaN in analyses where the source did not report a value, with Association Status = Missing. This is the same semantics as reference-panel variants where imputation fails.
