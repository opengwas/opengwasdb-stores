"""The catalog's tables: their columns, types and DDL (ADR 0039, epic #313).

Store-level facts live in `stores` once and are never repeated per Analysis. The
derived columns, counts, indexes and full-text index are built after the rows
are in (`INDEXES`, `SEARCH`), which is faster than maintaining them per insert.
"""

from __future__ import annotations

# column -> SQLite type, in table order.
STORE_COLUMNS: dict[str, str] = {
    "store_id": "TEXT PRIMARY KEY",
    "label": "TEXT",
    "description": "TEXT",
    "status": "TEXT",
    "superseded_by": "TEXT",
    "access_posture": "TEXT",
    "layout": "TEXT",
    "completion_state": "TEXT",
    "reference_assembly": "TEXT",
    "association_coverage": "TEXT",
    "n_analyses": "INTEGER",
    "n_variants": "INTEGER",
    "n_associations": "INTEGER",
    "store_bytes": "INTEGER",
    "format_version": "TEXT",
    "opengwasdb_version": "TEXT",
    "source_snapshot_id": "TEXT",
    "accepted_at": "TEXT",
    "validated_at": "TEXT",
    "notes": "TEXT",
}

# The Release Manifest columns the catalog carries, in table order, after `id`,
# `store_id`, `analysis_id` and `status`. A column a bundle lacks is NULL.
MANIFEST_COLUMNS: dict[str, str] = {
    "analysis_label": "TEXT",
    "source_label": "TEXT",
    "source_analysis_id": "TEXT",
    "trait_ontology_id": "TEXT",
    "trait_ontology_label": "TEXT",
    "trait_ontology_mapping_method": "TEXT",
    "tissue": "TEXT",
    "context": "TEXT",
    "trait_chr": "TEXT",
    "trait_bp": "INTEGER",
    "assigned_ancestry": "TEXT",
    "ancestry_assignment_method": "TEXT",
    "source_ancestry_label": "TEXT",
    "sample_size_kind": "TEXT",
    "sample_size_scope": "TEXT",
    "sample_size": "INTEGER",
    "n_cases": "INTEGER",
    "n_controls": "INTEGER",
    "stored_effect_scale": "TEXT",
    "original_effect_scale": "TEXT",
    "original_sd": "REAL",
    "original_sd_method": "TEXT",
    "maf_threshold": "REAL",
    "info_score_threshold": "REAL",
    "license": "TEXT",
    "publication_doi": "TEXT",
    "publication_pmid": "TEXT",
    "consortium": "TEXT",
    "first_author": "TEXT",
}

# Derived once at build time from the manifest columns (`policy.outcome_type`,
# `policy.trait_class`); the counts group by them.
DERIVED_COLUMNS: dict[str, str] = {
    "outcome_type": "TEXT CHECK (outcome_type IN ('binary', 'continuous'))",
    "trait_class": "TEXT NOT NULL CHECK (trait_class IN ('phenotype', 'molecular'))",
}

ANALYSIS_COLUMNS: dict[str, str] = {
    "id": "TEXT PRIMARY KEY",
    "store_id": "TEXT NOT NULL REFERENCES stores(store_id)",
    "analysis_id": "TEXT NOT NULL",
    "status": "TEXT NOT NULL",
    # 1 for an Analysis of a listed Store, 0 for a placeholder row.
    "listed": "INTEGER NOT NULL CHECK (listed IN (0, 1))",
    **MANIFEST_COLUMNS,
    **DERIVED_COLUMNS,
}

# The columns a listing can be sorted by, each with an index on (column, id) for
# `ORDER BY col, id` and keyset paging. `id` itself has a partial index of its own
# (`INDEXES`): the primary key covers placeholder rows too, so it cannot serve a
# listing of listed rows alone.
SORTABLE_COLUMNS: tuple[str, ...] = (
    "store_id", "analysis_label", "trait_ontology_label", "assigned_ancestry", "tissue",
    "stored_effect_scale", "sample_size",
)

# The dimensions `counts` holds, and the SQL expression each groups the listed Analyses by.
COUNT_DIMENSIONS: dict[str, str] = {
    "store": "a.store_id",
    "ancestry": "a.assigned_ancestry",
    "tissue": "a.tissue",
    "effect_scale": "a.stored_effect_scale",
    "outcome_type": "a.outcome_type",
    "trait_class": "a.trait_class",
    "coverage": "s.association_coverage",
}


def _create(table: str, columns: dict[str, str]) -> str:
    body = ",\n    ".join(f"{name} {kind}" for name, kind in columns.items())
    return f"CREATE TABLE {table} (\n    {body}\n)"


DDL: tuple[str, ...] = (
    _create("stores", STORE_COLUMNS),
    _create("analyses", ANALYSIS_COLUMNS),
    _create("selection", {
        "store_id": "TEXT PRIMARY KEY",
        "decision": "TEXT NOT NULL CHECK (decision IN ('listed', 'placeholder', 'excluded', 'refused'))",
        "reason": "TEXT NOT NULL",
    }),
    _create("catalog_meta", {"key": "TEXT PRIMARY KEY", "value": "TEXT"}),
    _create("counts", {"dimension": "TEXT NOT NULL", "value": "TEXT", "n_analyses": "INTEGER NOT NULL"}),
    "CREATE VIEW listed_analyses AS SELECT * FROM analyses WHERE listed = 1",
)

# Partial indexes over listed rows: a placeholder row costs a listing nothing.
# Queries go through `listed_analyses`, which carries `listed = 1`.
INDEXES: tuple[str, ...] = (
    *(
        f"CREATE INDEX analyses_by_{column} ON analyses ({column}, id) WHERE listed = 1"
        for column in SORTABLE_COLUMNS
    ),
    "CREATE INDEX analyses_by_id ON analyses (id) WHERE listed = 1",
    "CREATE INDEX analyses_by_region ON analyses (trait_chr, trait_bp, id) WHERE listed = 1",
    "CREATE INDEX counts_lookup ON counts (dimension, value)",
)

# External-content full-text index keyed by the `analyses` rowid; only listed rows are inserted.
SEARCH: tuple[str, ...] = (
    "CREATE VIRTUAL TABLE analysis_search USING fts5("
    "analysis_label, trait_ontology_label, consortium, first_author, "
    "content='analyses', content_rowid='rowid')",
    "INSERT INTO analysis_search (rowid, analysis_label, trait_ontology_label, consortium, first_author) "
    "SELECT rowid, analysis_label, trait_ontology_label, consortium, first_author "
    "FROM analyses WHERE listed = 1",
)
