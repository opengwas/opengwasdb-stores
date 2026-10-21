"""The catalog's tables: their columns, types and DDL (ADR 0039, epic #313).

Store-level facts live in `stores` once and are never repeated per Analysis. The
derived tables, indexes and full-text index that #324 adds belong in `DDL`.
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

ANALYSIS_COLUMNS: dict[str, str] = {
    "id": "TEXT PRIMARY KEY",
    "store_id": "TEXT NOT NULL REFERENCES stores(store_id)",
    "analysis_id": "TEXT NOT NULL",
    "status": "TEXT NOT NULL",
    **MANIFEST_COLUMNS,
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
)
