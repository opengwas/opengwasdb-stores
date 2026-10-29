# Complete ragged regions within retained boundaries

For Ragged Cis-and-Signals molecular Stores, reference completion is bounded to retained cis regions and significant trans regions. These regions are completed to reference-panel variants within their existing boundaries only; singleton suggestive associations remain as observed associations and are not region-expanded.

Ragged Reference-Completed stores use the same association sequence for observed, imputed, and missing reference variants within each completed region. Each completed region contains the full slice of Reference Variant Set variants within that boundary, plus observed off-panel variants inside the same boundary. Missing reference variants are retained as NaN statistic rows rather than omitted, keeping query semantics consistent with Dense Reference-Completed stores.

Observed off-panel variants inside a Ragged Reference-Completed region have ordinary observed Association Status and are not labelled as a separate Query Component.

