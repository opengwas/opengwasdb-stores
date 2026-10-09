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
  3. The records rebuilt and registered through the workflow keep their
     observed measurements exactly: OGS-00001..3, and OGS-00005 since issue
     #195 rebuilt and re-registered it with `pixi run release --config
     force=1`. The one correction to OGS-00001..3 since is their
     `format_version`, a Release Erratum (issue #195): `register` recorded its
     own fabricated default "1.0", and the true value is the "0.1.0" each
     Store's manifest declares.
  4. The three remaining migrated records (OGS-00004, OGS-00006, OGS-00007)
     record every measurement they do not have as `null` rather than inventing
     one. OGS-00005 was the fourth until its re-registration replaced its
     migrated record with `register`'s own output. Only the release-level
     verdict is carried, as `observed.validate_status`, because it is already
     recorded as the record's own `status`. Measurements live in the bundle's
     `sidecars/build_report.tsv`, but that describes a build the Validation
     Record did not observe and `register` never reads it, so promoting it into
     `observed` would launder a fact about a different run (issue #122).
  5. `observed.validate_status` agrees with the record's own `status`, which is
     the only value the master list publishes (issue #124).
  6. A recorded `observed.format_version` is MAJOR.MINOR.PATCH, the only shape
     opengwasdb stamps (its ADR 0041). A two-component value such as "1.0" is
     never a version these Stores carry: it names a retired pre-reset encoding.
  7. `observed` carries every `OBSERVED_FIELDS` key, and beyond them only the
     keys `register` documents writing when they apply, with a documented value:
     `variant_reference` (issue #148, docs/release-metadata-schema.md) and
     `resumed` (docs/spec/store-release-workflow.md). A record from a recipe
     naming a variant reference, as OGS-00005's re-registration is, carries
     `variant_reference` legitimately. Any other extra key is not the register
     shape.

Run from the repository root:
    pixi run python tests/validation-record/test_validation_record.py
"""

from __future__ import annotations

import re
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
# Rebuilt and re-registered through the release workflow's force path (#195);
# their record is `register`'s own output and names the archived release.
REREGISTERED: tuple[str, ...] = ("OGS-00005",)
MIGRATED: tuple[str, ...] = ("OGS-00004", "OGS-00006", "OGS-00007")

# The observed keys `register` writes only when they apply, with the values each
# may take: `variant_reference` (issue #148, docs/release-metadata-schema.md)
# and `resumed` (docs/spec/store-release-workflow.md). Every other observed key
# is one of `OBSERVED_FIELDS`, which every record carries.
CONDITIONAL_OBSERVED_VALUES: dict[str, frozenset[object]] = {
    "variant_reference": frozenset({"provided", "extracted"}),
    "resumed": frozenset({True}),
}

# The adapter ADR 0023 deleted. A Validation Record must never name it.
DELETED_ADAPTERS: tuple[str, ...] = ("build-store.py",)

# The only shape opengwasdb stamps a Store's format_version in (its ADR 0041).
FORMAT_VERSION_SHAPE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

# Observed measurements of the three records rebuilt through the workflow.
# Pinned so a later change that rewrites or drops them fails here. The
# fabricated OGS-00003 `n_associations` (n_variants x n_analyses, issue #122)
# is preserved as-is: this ticket migrates the container and must not make that
# number look more credible by recomputing it.
#
# `format_version` is pinned at its corrected value. `register` recorded "1.0",
# its own default when no step reported a version, and issue #195 removed that
# default. Each Store's manifest.json declares "0.1.0", the only format the
# recorded opengwasdb revisions wrote; the correction is a Release Erratum in
# each release.yaml.
GOLDEN_OBSERVED: dict[str, dict[str, object]] = {
    "OGS-00001": {
        "format_version": "0.1.0",
        "n_analyses": 10,
        "n_variants": 86376,
        "n_associations": 86373,
        "store_bytes": None,
        "build_elapsed_s": 4.677,
        "validate_status": "passed",
    },
    "OGS-00002": {
        "format_version": "0.1.0",
        "n_analyses": 10,
        "n_variants": 207764,
        "n_associations": 207761,
        "store_bytes": None,
        "build_elapsed_s": 74.38,
        "validate_status": "passed",
    },
    "OGS-00003": {
        "format_version": "0.1.0",
        "n_analyses": 10,
        "n_variants": 21230615,
        "n_associations": 212306150,
        "store_bytes": None,
        "build_elapsed_s": 1730.458,
        "validate_status": "passed",
    },
    # Rebuilt at opengwasdb 7b0c42d and re-registered by `register` on 8 Oct
    # 2026 (#195). Null is what no step reported: the build and validate
    # records carry no format_version, association count or store size, and
    # `register` no longer invents any of them. `variant_reference: provided`
    # is the documented conditional key for a recipe naming an existing panel.
    "OGS-00005": {
        "format_version": None,
        "n_analyses": 10,
        "n_variants": 14763864,
        "n_associations": None,
        "store_bytes": None,
        "build_elapsed_s": 1819.797,
        "validate_status": "passed_with_warnings",
        "variant_reference": "provided",
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
        """Each record carries the register-written validator, environment and observed keys.

        `observed` may also carry a conditional key `register` documents, with a
        documented value, and nothing else.
        """
        for store_id in STORES:
            record = self.load(store_id)

            for key in ("status", "validated_at", "validator", "build_environment", "observed", "checks"):
                self.assertIn(key, record, f"{store_id} is missing the register key {key!r}")

            self.assertEqual(
                set(record["build_environment"]),
                set(BUILD_ENVIRONMENT_FIELDS),
                f"{store_id}.build_environment must carry exactly the register keys",
            )
            observed = record["observed"]
            self.assertEqual(
                set(OBSERVED_FIELDS) - set(observed),
                set(),
                f"{store_id}.observed must carry every register key, with absent "
                "measurements recorded as null",
            )
            extra = set(observed) - set(OBSERVED_FIELDS)
            self.assertEqual(
                extra - set(CONDITIONAL_OBSERVED_VALUES),
                set(),
                f"{store_id}.observed carries keys register does not document writing",
            )
            for key in sorted(extra):
                self.assertIn(
                    observed[key],
                    CONDITIONAL_OBSERVED_VALUES[key],
                    f"{store_id}.observed.{key} is {observed[key]!r}, not a documented value",
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
        """Every record registered through the workflow keeps its observed block, exactly pinned.

        OGS-00001..3 are pinned with the issue-#195 erratum applied, so their
        `format_version` is "0.1.0"; OGS-00005 is pinned as its #195
        re-registration recorded it.
        """
        self.assertEqual(set(GOLDEN_OBSERVED), set(ALREADY_REGISTERED) | set(REREGISTERED))
        for store_id, expected in GOLDEN_OBSERVED.items():
            self.assertEqual(
                self.load(store_id)["observed"],
                expected,
                f"{store_id}.observed changed; a registered record changes only "
                "through a Release Erratum or another registration",
            )

    def test_reregistered_records_name_the_release_they_replaced(self) -> None:
        """A forced re-registration records where the replaced release was archived (#195)."""
        for store_id in REREGISTERED:
            replaced = self.load(store_id).get("replaced") or {}
            archive = str(replaced.get("archive", ""))
            self.assertRegex(
                archive,
                rf"/{store_id}/replaced/[0-9]{{8}}T[0-9]{{6}}Z$",
                f"{store_id} must name the replaced/<UTC>/ archive of the release it replaced",
            )
            self.assertTrue(replaced.get("replaced_at"), f"{store_id} must record when it replaced it")

    def test_recorded_format_versions_are_ones_opengwasdb_stamps(self) -> None:
        """Every committed record's format_version is MAJOR.MINOR.PATCH or null, never a two-part name."""
        recorded = sorted(path.parent.name for path in (REPO_ROOT / "stores").glob("OGS-*/validation.yaml"))
        self.assertTrue(set(STORES) <= set(recorded), "every pinned store must have a committed record")
        for store_id in recorded:
            # A record with no observed block records no version.
            version = (self.load(store_id).get("observed") or {}).get("format_version")
            if version is None:
                continue
            self.assertIsInstance(version, str, f"{store_id}.observed.format_version must be a string")
            self.assertIsNotNone(
                FORMAT_VERSION_SHAPE.fullmatch(version),
                f"{store_id}.observed.format_version {version!r} is not MAJOR.MINOR.PATCH; "
                "a two-component value names a retired pre-reset encoding, not this Store's",
            )

    def test_migrated_records_record_absence_rather_than_inventing(self) -> None:
        """The migrated records record every unrecorded measurement as null, never a value."""
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
