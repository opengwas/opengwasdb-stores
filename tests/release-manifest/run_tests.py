#!/usr/bin/env python3
"""Byte-equivalence of the shared builder-manifest module against the three
``build-store.py`` adapters (issue #96).

`resources/lib/release_manifest.py` replaces the manifest translation that was
copy-pasted into `opengwas-gwas-vcf-dense/build-store.py`,
`gwas-ssf-hybrid/build-store.py` and `gwas-ssf-ragged/build-store.py`. The
adapters stay in the tree (#103 deletes them), so this suite keeps them as the
oracle: for every already-built Store Release, regenerating the manifest through
the shared module must reproduce the adapter's bytes exactly.

The Ragged adapter is the degenerate case -- it hands the release's
`analyses.tsv` straight to `build_ragged_from_ssf`, so the manifest it produces
is that file unchanged, and equivalence is asserted against its bytes.
`eqtlgen-cis-pilot` is skipped: it is built from BESD through
`eqtlgen-besd-ragged/generate.py`, not by any of the three adapters.

Beyond the byte diff, the suite pins the behaviours the ticket names: excluded
rows are omitted, reader capability/assembly travel per row so a GRCh38 source
is not re-lifted, ancestry-proportion columns are discovered from the data, and
the canonical (lossless) representation keeps the six Hybrid columns the legacy
projection omits on purpose (issue #82, deferred to #104).

Run from the repository root:
    python3 tests/release-manifest/run_tests.py
"""
from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from resources.lib import release_manifest  # noqa: E402
from resources.lib.release_yaml import (  # noqa: E402
    read_release_yaml,
    read_tsv,
    require_text,
)

# The six Analytical Metadata columns `gwas-ssf-hybrid/build-store.py` never
# writes (issue #82). They are named here so the regression that documents the
# deferred loss cannot silently grow or shrink.
HYBRID_OMITTED_COLUMNS = (
    "sample_size_kind",
    "sample_size_scope",
    "n_cases",
    "n_controls",
    "original_effect_scale",
    "ancestry_assignment_method",
)


def load_adapter(module_name: str, relative_path: str) -> object:
    """Import a build-store.py adapter as the oracle of what it writes."""
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_layout(build: dict) -> str | None:
    """The shared-module layout for a release, from its declared builder.

    ``None`` for a release no ``build-store.py`` adapter builds (BESD -> Ragged).
    """
    entrypoint = require_text(build, "builder", "entrypoint")
    if "build_dense_from_vcf_manifest" in entrypoint or "complete_dense_store" in entrypoint:
        return "dense"
    if "build_hybrid_from_vcf_manifest" in entrypoint or "complete_hybrid_store" in entrypoint:
        return "hybrid"
    if entrypoint == "opengwasdb.layouts.ragged.build_ssf:build_ragged_from_ssf":
        return "ragged"
    return None


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def vcf_manifest_bytes(
    rows: list[dict[str, str]], layout: str, release_dir: Path, adapter: object
) -> tuple[bytes, bytes]:
    """(adapter manifest bytes, shared-module manifest bytes) for Dense/Hybrid."""
    assert layout in {"dense", "hybrid"}, layout
    build = read_release_yaml(release_dir / "build.yaml")
    buildable = release_manifest.buildable_rows(rows)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if layout == "dense":
            adapter.write_builder_manifest(buildable, tmp_path / "adapter.tsv")  # type: ignore[attr-defined]
            release_manifest.write_builder_manifest(rows, tmp_path / "shared.tsv", layout=layout)
        else:
            capability = require_text(build, "source", "source_reader_capability")
            assembly = require_text(build, "normalisation", "source_assembly")
            adapter.write_builder_manifest(  # type: ignore[attr-defined]
                buildable, capability, assembly, tmp_path / "adapter.tsv"
            )
            release_manifest.write_builder_manifest(
                rows,
                tmp_path / "shared.tsv",
                layout=layout,
                release_reader_capability=capability,
                release_source_assembly=assembly,
            )
        return read_bytes(tmp_path / "adapter.tsv"), read_bytes(tmp_path / "shared.tsv")


def main() -> None:
    n_checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal n_checks
        n_checks += 1
        if not condition:
            raise AssertionError(message)

    dense_adapter = load_adapter(
        "dense_build_store", "resources/generators/opengwas-gwas-vcf-dense/build-store.py"
    )
    hybrid_adapter = load_adapter(
        "hybrid_build_store", "resources/generators/gwas-ssf-hybrid/build-store.py"
    )

    # --- Byte-equivalence against each adapter, over every already-built release ---
    covered = {"dense": 0, "hybrid": 0, "ragged": 0}
    skipped: list[str] = []
    for build_path in sorted((REPO_ROOT / "families").glob("*/releases/*/build.yaml")):
        release_dir = build_path.parent
        analyses_path = release_dir / "analyses.tsv"
        if not analyses_path.exists():
            continue
        label = str(release_dir.relative_to(REPO_ROOT / "families"))
        layout = release_layout(read_release_yaml(build_path))
        if layout is None:
            skipped.append(label)
            continue
        rows = read_tsv(analyses_path)
        adapter = dense_adapter if layout == "dense" else hybrid_adapter

        if layout == "ragged":
            with tempfile.TemporaryDirectory() as tmp:
                shared_path = Path(tmp) / "shared.tsv"
                release_manifest.write_builder_manifest(rows, shared_path, layout="ragged")
                shared = read_bytes(shared_path)
            expected = read_bytes(analyses_path)
        else:
            expected, shared = vcf_manifest_bytes(rows, layout, release_dir, adapter)

        check(
            shared == expected,
            f"{label}: shared {layout} manifest differs from the adapter's "
            f"({len(shared)} vs {len(expected)} bytes)",
        )
        covered[layout] += 1

    check(covered["dense"] > 0, "no Dense release was exercised")
    check(covered["hybrid"] > 0, "no Hybrid release was exercised")
    check(covered["ragged"] > 0, "no Ragged release was exercised")
    check(
        skipped == ["eqtlgen-cis-pilot/releases/pilot-10", "eqtlgen-cis-pilot/releases/pilot-10-completed"],
        f"unexpected releases skipped: {skipped}",
    )

    # --- Excluded rows are omitted, matching adapter behaviour ---
    hybrid_release = REPO_ROOT / "families/gwas-catalog-eur-hybrid/releases/eur-hybrid-pilot-10"
    hybrid_rows = read_tsv(hybrid_release / "analyses.tsv")
    excluded_ids = {
        row["analysis_id"] for row in hybrid_rows if row.get("exclude_from_build") == "true"
    }
    check(excluded_ids == {"GCST003566"}, f"expected GCST003566 excluded, saw {sorted(excluded_ids)}")
    check(len(hybrid_rows) == 10, "the hybrid pilot should carry 10 registry rows")
    buildable = release_manifest.buildable_rows(hybrid_rows)
    check(len(buildable) == 9, f"expected 9 buildable rows, got {len(buildable)}")
    hybrid_manifest = release_manifest.builder_manifest(
        hybrid_rows,
        layout="hybrid",
        release_reader_capability="opengwasdb.gwas-ssf",
        release_source_assembly="hg38",
    )
    check(len(hybrid_manifest.rows) == 9, "the hybrid manifest must carry the 9 buildable rows")
    check(
        excluded_ids.isdisjoint({row["trait_id"] for row in hybrid_manifest.rows}),
        "an excluded analysis must not appear in the manifest",
    )

    # A Dense release with exclusions exercises the same behaviour on that path.
    dense_excluded_release = (
        REPO_ROOT / "families/ukb-b/releases/dense-observed-vcf-c128-resolved"
    )
    dense_excluded_rows = read_tsv(dense_excluded_release / "analyses.tsv")
    n_excluded = sum(1 for row in dense_excluded_rows if row.get("exclude_from_build") == "true")
    check(n_excluded == 3, f"expected 3 excluded ukb-b rows, got {n_excluded}")
    dense_manifest = release_manifest.builder_manifest(
        dense_excluded_rows, layout="dense"
    )
    check(
        len(dense_manifest.rows) == len(dense_excluded_rows) - 3,
        "the dense manifest must drop exactly the excluded rows",
    )

    # --- Per-row reader capability and source assembly survive the translation ---
    finngen_rows = read_tsv(REPO_ROOT / "families/finngen-r13/releases/r13-pilot-20/analyses.tsv")
    finngen_manifest = release_manifest.builder_manifest(finngen_rows, layout="dense")
    check(
        all(row["source_reader_capability"] == "opengwasdb.finngen-r13" for row in finngen_manifest.rows),
        "every finngen row must carry its own reader capability",
    )
    check(
        all(row["source_assembly"] == "GRCh38" for row in finngen_manifest.rows),
        "a GRCh38 source must declare GRCh38 in the manifest, so it is not re-lifted (opengwasdb#85)",
    )
    ukb_rows = read_tsv(REPO_ROOT / "families/ukb-b/releases/dense-observed-vcf-pilot-10/analyses.tsv")
    ukb_manifest = release_manifest.builder_manifest(ukb_rows, layout="dense")
    check(
        all(row["source_assembly"] == "GRCh37" for row in ukb_manifest.rows),
        "a GRCh37 source must declare GRCh37 in the manifest",
    )
    # ukb-b carries no source_reader_capability column: the column must still be
    # written (the adapter does), and stay blank rather than be invented here.
    check(
        all(row["source_reader_capability"] == "" for row in ukb_manifest.rows),
        "an absent reader capability must stay blank, never inferred",
    )
    # Hybrid overrides both with the release-level build.yaml declaration, which
    # is what its adapter does.
    check(
        all(row["source_reader_capability"] == "opengwasdb.gwas-ssf" for row in hybrid_manifest.rows)
        and all(row["source_assembly"] == "hg38" for row in hybrid_manifest.rows),
        "the hybrid projection must use build.yaml's reader capability/assembly for every row",
    )

    # --- Ancestry proportions are discovered from the data, not hardcoded ---
    finngen_ancestry = sorted(
        column for column in finngen_manifest.fieldnames if column.startswith("ancestry_prop_")
    )
    check(
        finngen_ancestry == [
            "ancestry_prop_AFR", "ancestry_prop_AMR", "ancestry_prop_EAS", "ancestry_prop_EUR",
            "ancestry_prop_MID", "ancestry_prop_NAF", "ancestry_prop_SAS",
        ],
        f"finngen's ancestry columns were not discovered in order: {finngen_ancestry}",
    )
    check(
        finngen_manifest.fieldnames == [*release_manifest.CANONICAL_COLUMNS, *finngen_ancestry],
        "discovered ancestry columns must be appended after the canonical column set",
    )
    check(
        finngen_manifest.rows[0]["ancestry_prop_EUR"] == finngen_rows[0]["ancestry_prop_EUR"],
        "a discovered ancestry proportion must be carried through verbatim",
    )
    synthetic = [{"analysis_id": "a", "source_file": "f", "analysis_label": "l", "sample_size": "1",
                  "stored_effect_scale": "sd", "original_sd_method": "source_provided",
                  "ancestry_prop_XYZ": "0.5", "ancestry_prop_ABC": "0.5"}]
    synthetic_manifest = release_manifest.canonical_manifest(synthetic)
    check(
        [c for c in synthetic_manifest.fieldnames if c.startswith("ancestry_prop_")]
        == ["ancestry_prop_ABC", "ancestry_prop_XYZ"],
        "a population the registry has never seen must still be discovered, sorted",
    )

    # --- The canonical representation is lossless; the hybrid projection is not ---
    canonical_hybrid = release_manifest.canonical_manifest(
        hybrid_rows,
        release_reader_capability="opengwasdb.gwas-ssf",
        release_source_assembly="hg38",
    )
    # The Hybrid omission is deliberate legacy compatibility debt (issue #82):
    # the canonical representation keeps every column; the adapter-compatible
    # projection keeps dropping the six until #104 adopts the lossless one.
    for column in HYBRID_OMITTED_COLUMNS:
        check(
            column in canonical_hybrid.fieldnames,
            f"the canonical representation must retain {column} (issue #82)",
        )
        check(
            column not in hybrid_manifest.fieldnames,
            f"the legacy hybrid projection must keep omitting {column} until #104",
        )
    check(
        len(HYBRID_OMITTED_COLUMNS) == 6,
        "exactly the six columns issue #82 names are deferred",
    )
    check(
        canonical_hybrid.fieldnames == [
            *release_manifest.CANONICAL_COLUMNS,
            *sorted(c for c in release_manifest.ancestry_proportion_columns(hybrid_rows)),
        ],
        "the canonical representation is the full Dense-shaped column set",
    )
    registry_by_id = {row["analysis_id"]: row for row in hybrid_rows}
    for translated in canonical_hybrid.rows:
        source = registry_by_id[translated["trait_id"]]
        for column, registry_column in (
            ("sample_size_kind", "sample_size_kind"),
            ("n_cases", "n_cases"),
            ("original_effect_scale", "original_effect_scale"),
            ("ancestry_assignment_method", "ancestry_assignment_method"),
        ):
            check(
                translated[column] == source[registry_column],
                f"{translated['trait_id']}: canonical {column} must equal the registry value",
            )
    check(
        any(canonical_hybrid.rows[i]["n_cases"] for i in range(len(canonical_hybrid.rows))),
        "the hybrid pilot's case/control counts must survive the canonical translation",
    )
    # A row with no per-row capability/assembly falls back to the release-level
    # declaration; a row that has one keeps it.
    fallback = release_manifest.canonical_manifest(
        [row for row in ukb_rows[:1]],
        release_reader_capability="opengwasdb.gwas-vcf",
        release_source_assembly="hg19",
    )
    check(
        fallback.rows[0]["source_reader_capability"] == "opengwasdb.gwas-vcf",
        "a blank per-row capability must be fillable from the release-level declaration",
    )
    check(
        fallback.rows[0]["source_assembly"] == "GRCh37",
        "a present per-row assembly must win over the release-level declaration",
    )

    # --- Failure modes are explicit ---
    try:
        release_manifest.builder_manifest(finngen_rows, layout="sparse")
    except ValueError as error:
        check("unknown Store Layout" in str(error), f"unhelpful layout error: {error}")
    else:
        raise AssertionError("an unknown Store Layout must raise ValueError")
    try:
        release_manifest.canonical_row({"analysis_id": "a"})
    except KeyError as error:
        check("source_file" in str(error), f"unhelpful required-column error: {error}")
    else:
        raise AssertionError("a row missing required registry columns must raise KeyError")

    # --- The written TSV is parseable back to the manifest it came from ---
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "manifest.tsv"
        written = release_manifest.write_builder_manifest(
            finngen_rows, out, layout="dense"
        )
        with out.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            check(list(reader.fieldnames or []) == written.fieldnames, "header must round-trip")
            reparsed = list(reader)
        check(reparsed == written.rows, "rows must round-trip")

    print(
        f"byte-equivalence: {covered['dense']} Dense, {covered['hybrid']} Hybrid, "
        f"{covered['ragged']} Ragged releases vs their adapters"
    )
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
