#!/usr/bin/env python3
"""Validation Record shape tests (issue #135).

Two incompatible Validation Record shapes used to coexist: the pre-seam record
written by the deleted `build-store.py` adapters, and the shape `register`
writes (issue #119). Issue #135 migrates the remainder onto the register shape
so the registry has one format.

This suite asserts the container migration:

  1. Every committed record carries the register-written keys: `validator`,
     `build_environment`, `observed`, `checks`, `reports`, `warnings`,
     `errors`.
  2. No record names a deleted generator adapter (`build-store.py`) as its
     validator; every record names `opengwasdb validate`, and its version is
     the `opengwasdb` revision the record's build_environment records.
  3. The three records already rebuilt and re-registered (OGS-00001..3) keep
     their observed measurements exactly.
  4. The four migrated records (OGS-00004..7) record every measurement they do
     not have as `null` rather than inventing one. Only the release-level
     verdict is carried, as `observed.validate_status`, because it is already
     recorded as the record's own `status`. Measurements live in the bundle's
     `sidecars/build_report.tsv`, but that describes a build the Validation
     Record did not observe and `register` never reads it, so promoting it into
     `observed` would launder a fact about a different run (issue #122).
  5. `observed.validate_status` agrees with the record's own `status`, which is
     the only value the master list publishes (issue #124).

Run from the repository root:
    pixi run python tests/validation-record/test_validation_record.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ogstores.register import (
    BUILD_ENVIRONMENT_FIELDS,
    OBSERVED_FIELDS,
    VALIDATOR_NAME,
)

STORES: tuple[str, ...] = tuple(f"OGS-{i:05d}" for i in range(1, 8))
ALREADY_REGISTERED: tuple[str, ...] = ("OGS-00001", "OGS-00002", "OGS-00003")
MIGRATED: tuple[str, ...] = ("OGS-00004", "OGS-00005", "OGS-00006", "OGS-00007")

# The adapter ADR 0023 deleted. A Validation Record must never name it.
DELETED_ADAPTERS: tuple[str, ...] = ("build-store.py",)

# Observed measurements of the three records rebuilt through the workflow.
# Pinned so a later change that rewrites or drops them fails here. The
# fabricated OGS-00003 `n_associations` (n_variants x n_analyses, issue #122)
# is preserved as-is: this ticket migrates the container and must not make that
# number look more credible by recomputing it.
GOLDEN_OBSERVED: dict[str, dict[str, object]] = {
    "OGS-00001": {
        "format_version": "1.0",
        "n_analyses": 10,
        "n_variants": 86376,
        "n_associations": 86373,
        "store_bytes": None,
        "build_elapsed_s": 4.677,
        "validate_status": "passed",
    },
    "OGS-00002": {
        "format_version": "1.0",
        "n_analyses": 10,
        "n_variants": 207764,
        "n_associations": 207761,
        "store_bytes": None,
        "build_elapsed_s": 74.38,
        "validate_status": "passed",
    },
    "OGS-00003": {
        "format_version": "1.0",
        "n_analyses": 10,
        "n_variants": 21230615,
        "n_associations": 212306150,
        "store_bytes": None,
        "build_elapsed_s": 1730.458,
        "validate_status": "passed",
    },
}


class TestCommittedValidationRecords(unittest.TestCase):
    """Every committed record is the register shape, and says only what it recorded."""

    def load(self, store_id: str) -> dict:
        path = REPO_ROOT / "stores" / store_id / "validation.yaml"
        self.assertTrue(path.is_file(), f"{store_id} validation.yaml must exist")
        with path.open(encoding="utf-8") as fh:
            record = yaml.safe_load(fh)
        self.assertIsInstance(record, dict, f"{store_id} validation.yaml must be a mapping")
        return record

    def test_every_record_is_the_register_shape(self) -> None:
        """Each record carries the exact register-written validator, environment and observed keys."""
        for store_id in STORES:
            record = self.load(store_id)

            for key in ("status", "validated_at", "validator", "build_environment", "observed", "checks"):
                self.assertIn(key, record, f"{store_id} is missing the register key {key!r}")

            self.assertEqual(
                set(record["build_environment"]),
                set(BUILD_ENVIRONMENT_FIELDS),
                f"{store_id}.build_environment must carry exactly the register keys",
            )
            self.assertEqual(
                set(record["observed"]),
                set(OBSERVED_FIELDS),
                f"{store_id}.observed must carry exactly the register keys, with absent "
                "measurements recorded as null",
            )
            self.assertEqual(
                record["validator"]["name"],
                VALIDATOR_NAME,
                f"{store_id} must name the register validator",
            )

    def test_no_record_names_a_deleted_adapter_as_validator(self) -> None:
        """No Validation Record points its provenance at an adapter ADR 0023 deleted."""
        for store_id in STORES:
            name = str(self.load(store_id)["validator"]["name"])
            for adapter in DELETED_ADAPTERS:
                self.assertNotIn(
                    adapter,
                    name,
                    f"{store_id} names the deleted adapter {adapter!r} as its validator",
                )

    def test_validator_version_is_the_recorded_opengwasdb_revision(self) -> None:
        """The validator version is the opengwasdb revision the record's build_environment names."""
        for store_id in STORES:
            record = self.load(store_id)
            commit = record["build_environment"].get("opengwasdb_commit")
            self.assertTrue(commit, f"{store_id} must record an opengwasdb commit")
            self.assertEqual(
                record["validator"]["version"],
                f"opengwasdb@{commit}",
                f"{store_id}.validator.version must name the recorded opengwasdb revision",
            )

    def test_already_registered_records_keep_their_observed_measurements(self) -> None:
        """OGS-00001..3 are untouched by this ticket: their observed block is exactly pinned."""
        for store_id, expected in GOLDEN_OBSERVED.items():
            self.assertEqual(
                self.load(store_id)["observed"],
                expected,
                f"{store_id}.observed changed; this ticket must leave the three "
                "already-registered records untouched",
            )

    def test_migrated_records_record_absence_rather_than_inventing(self) -> None:
        """OGS-00004..7 record every unrecorded measurement as null, never a value."""
        for store_id in MIGRATED:
            record = self.load(store_id)
            observed = record["observed"]
            for field in OBSERVED_FIELDS:
                if field == "validate_status":
                    continue
                self.assertIsNone(
                    observed[field],
                    f"{store_id}.observed.{field} must be null: the Validation Record never "
                    f"recorded it, so it is absent rather than invented (got {observed[field]!r})",
                )

    def test_migrated_records_preserve_their_recorded_evidence(self) -> None:
        """The container changes; the checks, reports, warnings and errors survive it."""
        for store_id in MIGRATED:
            record = self.load(store_id)
            self.assertIn("checks", record, store_id)
            self.assertIn("reports", record, store_id)
            self.assertIn("warnings", record, store_id)
            self.assertIn("errors", record, store_id)
            self.assertTrue(record["reports"].get("build_report"), f"{store_id} keeps its build report pointer")
            self.assertEqual(record["errors"], [], f"{store_id} recorded no blocking error")

    def test_validate_status_agrees_with_the_release_verdict(self) -> None:
        """The carried verdict matches the record's own status, the value the master list publishes."""
        for store_id in STORES:
            record = self.load(store_id)
            self.assertEqual(
                record["observed"]["validate_status"],
                record["status"],
                f"{store_id}.observed.validate_status must agree with the release-level status",
            )
            self.assertIn(
                record["status"],
                ("not_run", "passed", "failed", "passed_with_warnings"),
                f"{store_id}.status must be a documented verdict",
            )


if __name__ == "__main__":
    unittest.main()
