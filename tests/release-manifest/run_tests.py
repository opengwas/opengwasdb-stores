#!/usr/bin/env python3
"""The shared builder-manifest module reproduces what the three retired
``build-store.py`` adapters wrote (issue #96), byte for byte (issue #103).

`resources/lib/release_manifest.py` replaced the manifest translation that was
copy-pasted into `opengwas-gwas-vcf-dense/build-store.py`,
`gwas-ssf-hybrid/build-store.py` and `gwas-ssf-ragged/build-store.py`. Issue #96
proved equivalence by importing the adapters as the oracle; issue #103 deleted
those adapters, so their exact output bytes are pinned in
`adapter_manifest_sha256.json` (generated from the adapters before deletion) and
this suite keeps asserting the shared module reproduces them. The module is the
only manifest producer left; nothing else translates a release's
`analyses.tsv` for OpenGWASDB.

The Ragged adapter is the degenerate case -- it handed the release's
`analyses.tsv` straight to `build_ragged_from_ssf`, so the manifest it produced
is that file unchanged, and equivalence is asserted against its bytes.
`eqtlgen-cis-pilot` is skipped: it is built from BESD through
`eqtlgen-besd-ragged/generate.py`, not by any of the three adapters.

Since #104 the *live* Hybrid projection is the lossless canonical one, so the
adapter's 17-column output is compared through the retained
``LEGACY_HYBRID_PROJECTION``: that is the historical #96 evidence that the
retired adapter's bytes are still exactly recoverable.

Beyond the byte diff, the suite pins the behaviours the ticket names: excluded
rows are omitted, reader capability/assembly travel per row so a GRCh38 source
is not re-lifted, ancestry-proportion columns are discovered from the data, the
live Hybrid projection keeps the six Analytical Metadata columns the legacy
projection omits (issue #82, adopted by #104), and the legacy projection still
omits exactly those six as #96 evidence.

Run from the repository root:
    python3 tests/release-manifest/run_tests.py
"""
from __future__ import annotations

import csv
import hashlib
import json
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

#: The adapter output bytes the shared module must still reproduce, pinned from
#: the three retired adapters before #103 deleted them.
ADAPTER_GOLDEN_PATH = REPO_ROOT / "tests" / "release-manifest" / "adapter_manifest_sha256.json"

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


def load_adapter_golden() -> dict[str, dict]:
    """The pinned adapter output bytes, keyed by release label."""
    return json.loads(ADAPTER_GOLDEN_PATH.read_text(encoding="utf-8"))["releases"]


# The executable `build.command` each Store Layout's adapter builds (#97).
LAYOUT_BY_COMMAND = {
    "build-dense-vcf": "dense",
    "complete-dense": "dense",
    "build-hybrid": "hybrid",
    # The catalogue-routed Hybrid command (issue #104) still produces a Hybrid
    # Store, so its release is covered by the Hybrid adapter's legacy projection.
    "build-hybrid-from-catalogue": "hybrid",
    "complete-hybrid": "hybrid",
    "build-ragged-ssf": "ragged",
}


def release_layout(build: dict) -> str | None:
    """The shared-module layout for a release, from its declared operation.

    Accepts both schemas: #97 migrated the seven Trial Store Releases to
    ``build.command``, while the releases it did not migrate still carry the
    pre-#95 ``builder.entrypoint``. ``None`` for a release no ``build-store.py``
    adapter builds (BESD -> Ragged).
    """
    command = (build.get("build") or {}).get("command")
    if command in LAYOUT_BY_COMMAND:
        return LAYOUT_BY_COMMAND[command]
    if command:
        return None
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
    rows: list[dict[str, str]], layout: str, release_dir: Path
) -> bytes:
    """The shared module's Dense/Hybrid builder manifest bytes for one release."""
    assert layout in {"dense", "hybrid"}, layout
    build = read_release_yaml(release_dir / "build.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        shared_path = Path(tmp) / "shared.tsv"
        if layout == "dense":
            release_manifest.write_builder_manifest(rows, shared_path, layout=layout)
        else:
            # #104 flipped the *live* Hybrid projection to the lossless canonical
            # one, so the adapter's 17-column output is reproduced through the
            # retained ``LEGACY_HYBRID_PROJECTION`` -- the historical #96 evidence
            # that the retired adapter's bytes are still recoverable.
            release_manifest.write_builder_manifest(
                rows,
                shared_path,
                layout=layout,
                release_reader_capability=require_text(build, "source", "source_reader_capability"),
                release_source_assembly=require_text(build, "normalisation", "source_assembly"),
                projection=release_manifest.LEGACY_HYBRID_PROJECTION,
            )
        return read_bytes(shared_path)


def main() -> None:
    n_checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal n_checks
        n_checks += 1
        if not condition:
            raise AssertionError(message)

    golden = load_adapter_golden()

    # --- Byte-equivalence against the pinned adapter output, over every already-built release ---
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
        if label not in golden:
            raise AssertionError(f"{label}: no pinned adapter output in {ADAPTER_GOLDEN_PATH.name}")
        expected = golden[label]
        check(expected["layout"] == layout,
              f"{label}: pinned adapter layout {expected['layout']!r} != release layout {layout!r}")

        if layout == "ragged":
            shared = read_bytes(analyses_path)
        else:
            shared = vcf_manifest_bytes(rows, layout, release_dir)
        digest = hashlib.sha256(shared).hexdigest()

        check(
            len(shared) == expected["bytes"],
            f"{label}: shared {layout} manifest is {len(shared)} bytes, the retired "
            f"adapter's was {expected['bytes']}",
        )
        check(
            digest == expected["sha256"],
            f"{label}: shared {layout} manifest sha256 {digest} != the retired "
            f"adapter's {expected['sha256']}",
        )
        covered[layout] += 1

    check(covered["dense"] > 0, "no Dense release was exercised")
    check(covered["hybrid"] > 0, "no Hybrid release was exercised")
    check(covered["ragged"] > 0, "no Ragged release was exercised")
    check(
        skipped == ["eqtlgen-cis-pilot/releases/pilot-10", "eqtlgen-cis-pilot/releases/pilot-10-completed"],
        f"unexpected releases skipped: {skipped}",
    )

    # --- A single-use iterable is materialised once, not read twice (issue #96) ---
    # The Ragged projection needs the release table twice: once for its buildable
    # rows, once for the registry column names that become the manifest header.
    # A generator or csv.DictReader can only be read once, so builder_manifest()
    # must materialise it at its own API boundary. Before the accepted fix a
    # generator produced an empty header and an empty manifest.
    ragged_path = (
        REPO_ROOT / "families/metabolome-plasma-2023/releases/2023-chen-pilot-80/analyses.tsv"
    )
    ragged_rows = read_tsv(ragged_path)
    ragged_header = ragged_path.read_text(encoding="utf-8").split("\n", 1)[0].split("\t")
    check(
        all(row.get("exclude_from_build") != "true" for row in ragged_rows),
        "the ragged iterator regression assumes a release with no excluded rows",
    )
    list_manifest = release_manifest.builder_manifest(ragged_rows, layout="ragged")
    with ragged_path.open(newline="", encoding="utf-8") as handle:
        dictreader_manifest = release_manifest.builder_manifest(
            csv.DictReader(handle, delimiter="\t"), layout="ragged"
        )
    generator_manifest = release_manifest.builder_manifest(
        (row for row in ragged_rows), layout="ragged"
    )
    check(
        list_manifest.fieldnames == ragged_header,
        "the ragged manifest header must be the release's own analyses.tsv header",
    )
    for label, manifest in (
        ("generator", generator_manifest),
        ("csv.DictReader", dictreader_manifest),
    ):
        check(
            manifest.fieldnames == list_manifest.fieldnames,
            f"a {label} input must yield the same ragged header as a list input",
        )
        check(
            manifest.rows == list_manifest.rows,
            f"a {label} input must yield the same ragged rows as a list input",
        )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ragged-generator.tsv"
        written = release_manifest.write_builder_manifest(
            (row for row in ragged_rows), out, layout="ragged"
        )
        check(
            written.fieldnames == ragged_header and written.rows == list_manifest.rows,
            "a generator write_builder_manifest must match the list-input manifest",
        )
        check(
            read_bytes(out) == ragged_path.read_bytes(),
            "a generator ragged manifest must stay byte-identical to analyses.tsv",
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
    # Hybrid's live projection is lossless (issue #104): a blank per-row
    # capability is filled from build.yaml, while each row's own assembly wins.
    check(
        all(row["source_reader_capability"] == "opengwasdb.gwas-ssf" for row in hybrid_manifest.rows)
        and all(row["source_assembly"] == "GRCh38" for row in hybrid_manifest.rows),
        "the lossless hybrid projection must fill a blank capability but keep each row's assembly",
    )
    legacy_hybrid_manifest = release_manifest.builder_manifest(
        hybrid_rows,
        layout="hybrid",
        release_reader_capability="opengwasdb.gwas-ssf",
        release_source_assembly="hg38",
        projection=release_manifest.LEGACY_HYBRID_PROJECTION,
    )
    check(
        all(row["source_reader_capability"] == "opengwasdb.gwas-ssf" for row in legacy_hybrid_manifest.rows)
        and all(row["source_assembly"] == "hg38" for row in legacy_hybrid_manifest.rows),
        "the legacy hybrid projection must keep taking capability/assembly from build.yaml",
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

    # --- Live Hybrid is lossless; the legacy adapter projection is retained ---
    canonical_hybrid = release_manifest.canonical_manifest(
        hybrid_rows,
        release_reader_capability="opengwasdb.gwas-ssf",
        release_source_assembly="hg38",
    )
    # #104 adopted the lossless canonical representation for live Hybrid builds,
    # so the live projection and the canonical representation are now identical.
    check(
        hybrid_manifest.fieldnames == canonical_hybrid.fieldnames,
        "the live hybrid projection must be the lossless canonical column set (issue #104)",
    )
    check(
        hybrid_manifest.rows == canonical_hybrid.rows,
        "the live hybrid projection must carry every canonical row value verbatim",
    )
    # The six Analytical Metadata columns issue #82 recorded as lost are now kept
    # live; the legacy projection is retained purely as the #96 equivalence oracle.
    for column in HYBRID_OMITTED_COLUMNS:
        check(
            column in canonical_hybrid.fieldnames,
            f"the live representation must retain {column} (issue #82)",
        )
        check(
            column not in legacy_hybrid_manifest.fieldnames,
            f"the legacy hybrid projection must keep omitting {column} (#96 evidence)",
        )
    check(
        len(HYBRID_OMITTED_COLUMNS) == 6,
        "exactly the six columns issue #82 names were lost",
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
        f"byte-equivalence to the retired adapters: {covered['dense']} Dense, "
        f"{covered['hybrid']} Hybrid, {covered['ragged']} Ragged releases"
    )
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
