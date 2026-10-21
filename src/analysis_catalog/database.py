"""Write one catalog version: the SQLite database and its TSV exports (ADR 0039).

Every version is a full build into a fresh database, never an in-place update.
The TSV exports are read back from the tables, so they hold exactly their rows.
A NULL is an empty cell.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

from analysis_catalog import BUILDER_VERSION
from analysis_catalog.columns import (
    ANALYSIS_COLUMNS, COUNT_DIMENSIONS, DDL, INDEXES, SEARCH, STORE_COLUMNS,
)
from analysis_catalog.policy import LISTED, PLACEHOLDER, POLICY, Decision

DATABASE = "catalog.sqlite"
STORES_TSV = "stores.tsv"
ANALYSES_TSV = "analyses.tsv.gz"


def write_database(
    path: Path,
    decisions: Sequence[Decision],
    *,
    version: str,
    built_at: str,
    registry_commit: str | None,
) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement in DDL:
            connection.execute(statement)
        served = [d for d in decisions if d.decision in (LISTED, PLACEHOLDER)]
        _insert(connection, "stores", STORE_COLUMNS, (d.store for d in served if d.store))
        _insert(connection, "analyses", ANALYSIS_COLUMNS, (row for d in served for row in d.analyses))
        _derive(connection)
        connection.executemany(
            "INSERT INTO selection (store_id, decision, reason) VALUES (?, ?, ?)",
            [(d.store_id, d.decision, d.reason) for d in decisions],
        )
        connection.executemany(
            "INSERT INTO catalog_meta (key, value) VALUES (?, ?)",
            [
                ("version", version),
                ("built_at", built_at),
                ("registry_commit", registry_commit),
                ("builder_version", BUILDER_VERSION),
                ("serving_policy", json.dumps(list(POLICY))),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _derive(connection: sqlite3.Connection) -> None:
    """Counts, indexes and the full-text index, all over listed Analyses only."""
    for dimension, expression in COUNT_DIMENSIONS.items():
        connection.execute(
            "INSERT INTO counts (dimension, value, n_analyses) "
            f"SELECT ?, {expression}, COUNT(*) FROM analyses a JOIN stores s USING (store_id) "
            "WHERE a.listed = 1 GROUP BY 2",
            (dimension,),
        )
    for statement in (*INDEXES, *SEARCH):
        connection.execute(statement)


def _insert(
    connection: sqlite3.Connection, table: str, columns: dict[str, str], rows: Iterable[tuple]
) -> None:
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    connection.executemany(f"INSERT INTO {table} ({names}) VALUES ({marks})", rows)


def write_exports(version_dir: Path) -> None:
    """`stores.tsv` and `analyses.tsv.gz`: the tables' rows in key order."""
    connection = sqlite3.connect(f"file:{version_dir / DATABASE}?mode=ro", uri=True)
    try:
        with open(version_dir / STORES_TSV, "w", encoding="utf-8", newline="") as handle:
            _write_table(connection, "stores", "store_id", handle)
        with open(version_dir / ANALYSES_TSV, "wb") as raw:
            # mtime 0 and no filename: the same rows give the same bytes.
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
                with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as handle:
                    _write_table(connection, "analyses", "id", handle)
    finally:
        connection.close()


def _write_table(connection: sqlite3.Connection, table: str, key: str, handle: io.TextIOBase) -> None:
    cursor = connection.execute(f"SELECT * FROM {table} ORDER BY {key}")
    writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
    writer.writerow([column[0] for column in cursor.description])
    for row in cursor:
        writer.writerow(["" if value is None else value for value in row])
