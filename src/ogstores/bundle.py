"""Release Bundle ownership: read it, and check what the registry owns.

`check()` covers registry-side facts only -- required keys, `store_id` matching
the directory name, id format, declared files existing, checksums, `derived_from`
resolving, legal status transitions -- and delegates the `analyses.tsv` contract
to `opengwasdb.model.analyses.read_analyses` rather than reimplementing it
(ADR 0017). It never opens a Store.

It does assert that Phase B's columns are present and vocabulary-valid
(`assigned_ancestry`, `ancestry_assignment_method`, `stored_effect_scale`,
`original_sd_method`, ...). Asserting presence is registry-side structural
validation; recomputing the values would be Phase A writing `analyses.tsv`,
which it never does.
"""

from __future__ import annotations

raise NotImplementedError("Phase A implementation pending; see docs/spec/store-release-workflow.md")
