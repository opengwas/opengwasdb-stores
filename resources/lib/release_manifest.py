"""One builder-manifest translation for every Store Layout (issue #96).

OpenGWASDB's layout builders do not, in general, read this registry's
``analyses.tsv`` columns. They want OpenGWASDB's own vocabulary -- ``trait_id``,
``trait_name``, ``n`` -- from before the shared Analysis schema (OpenGWASDB ADR
0034) replaced it. That translation used to be copy-pasted into each
``build-store.py`` adapter; from #96 it lives here, once, and #103 deleted the
adapters. Its projections are still pinned byte-for-byte to what those adapters
wrote (``tests/release-manifest/``), because switching a live build to the
lossless representation changes what an accepted Store contains.

The module has three layers, deliberately separated:

``canonical_manifest``
    A lossless, adapter-independent translation of registry Analysis rows into
    builder-manifest columns. Every interpretation-bearing column is retained:
    Effect Scale, Original SD and its method, Assigned Ancestry and its Ancestry
    Assignment Method, ancestry proportions, Sample Size kind/scope and
    case/control counts, Trait Ontology Mapping, and Attribution. Both
    ``source_reader_capability`` and ``source_assembly`` are carried per row, so
    an already-GRCh38 source is not re-lifted (opengwasdb#85). Ancestry-proportion
    columns are discovered from the data, never hardcoded.

``ADAPTER_PROJECTIONS``
    The adapter-compatible serialisation of those rows: one named projection per
    Store Layout. These mirror the retired ``build-store.py`` adapters rather
    than the ideal, because the evidence for #96 was that regenerating a
    release's manifest reproduced what the corresponding adapter wrote byte for
    byte, and that guarantee is pinned in ``tests/release-manifest/``.

``write_builder_manifest``
    Registry Analysis rows in, adapter-compatible builder manifest TSV out.

Legacy Hybrid projection (issue #82, deferred to #104)
------------------------------------------------------
``build_hybrid_from_vcf_manifest`` shares the Dense builder's manifest shape,
but the retired ``gwas-ssf-hybrid/build-store.py`` wrote only 17 of Dense's 23
columns: it
omits ``sample_size_kind``, ``sample_size_scope``, ``n_cases``, ``n_controls``,
``original_effect_scale`` and ``ancestry_assignment_method``, and it takes the
row's source reader capability/assembly from ``build.yaml`` instead of from the
row. All six are Analytical Metadata (``CONTEXT.md``), so the built Hybrid
stores cannot carry them -- issue #82's metadata loss, which those releases'
own ``validation.yaml`` files record as warnings.

``ADAPTER_PROJECTIONS["hybrid"]`` reproduces that omission on purpose. #96's
contract is deduplication, not a change to what an already-accepted Store
Release contains; feeding the lossless columns into a live Hybrid build would
change the built Store, and that change belongs with the catalogue-path/rebuild
work (#104). Callers that want the lossless representation use
``canonical_manifest``, which retains all six columns, so nothing has to be
reconstructed from the registry table.

Ragged is not a VCF manifest at all: ``build_ragged_from_ssf`` reads the registry
Analysis column names (``analysis_index``, ``analysis_id``, ``filtered_file``,
...) straight out of ``analyses.tsv``, so that layout's projection is the
registry table verbatim. Store Families built from BESD
(``eqtlgen-cis-pilot``) go through ``build_ragged_from_besd`` via
``eqtlgen-besd-ragged/generate.py`` and are out of scope here.

Upstream: OpenGWASDB's Trait/Manifest vocabulary should eventually accept
``analyses.tsv`` directly, which would let this module shrink to a no-op. That
request is filed as https://github.com/opengwas/opengwasdb/issues/170 and linked
from this repository's issue #96.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# The canonical column contract
# ---------------------------------------------------------------------------

#: Registry Analysis row column each canonical builder-manifest column is read
#: from, in the order the Dense adapter writes them. One spelling per concept:
#: the left-hand names are OpenGWASDB's builder-manifest vocabulary, the
#: right-hand names are this registry's ``analyses.tsv`` vocabulary (ADR 0014).
CANONICAL_COLUMN_SOURCE: dict[str, str] = {
    "trait_id": "analysis_id",
    "file_path": "source_file",
    "trait_name": "analysis_label",
    "n": "sample_size",
    "stored_effect_scale": "stored_effect_scale",
    "source_reader_capability": "source_reader_capability",
    "source_assembly": "source_genome_build",
    "original_sd_method": "original_sd_method",
    "original_sd": "original_sd",
    "assigned_ancestry": "assigned_ancestry",
    "ancestry_assignment_method": "ancestry_assignment_method",
    "original_effect_scale": "original_effect_scale",
    "sample_size_kind": "sample_size_kind",
    "sample_size_scope": "sample_size_scope",
    "n_cases": "n_cases",
    "n_controls": "n_controls",
    "trait_ontology_id": "trait_ontology_id",
    "trait_ontology_label": "trait_ontology_label",
    "license": "license",
    "publication_doi": "publication_doi",
    "publication_pmid": "publication_pmid",
    "consortium": "consortium",
    "first_author": "first_author",
}

#: The canonical builder-manifest columns, in the Dense adapter's order.
CANONICAL_COLUMNS: tuple[str, ...] = tuple(CANONICAL_COLUMN_SOURCE)

#: Registry columns a translation cannot proceed without. The two VCF-manifest
#: adapters index these directly; every other column defaults to "" so a release
#: that legitimately omits one (e.g. ``n_cases`` for a quantitative trait) still
#: produces a manifest rather than a KeyError.
REQUIRED_REGISTRY_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "source_file",
    "analysis_label",
    "sample_size",
    "stored_effect_scale",
    "original_sd_method",
)

#: Prefix identifying a data-discovered ancestry-proportion column.
ANCESTRY_PROPORTION_PREFIX = "ancestry_prop_"

#: ``source_reader_capability``/``source_assembly`` are the two columns a layout
#: may take from ``build.yaml`` instead of from each row -- see the Hybrid
#: projection below. Their release-level names in ``build.yaml`` are
#: ``source.source_reader_capability`` and ``normalisation.source_assembly``.
RELEASE_SOURCED_COLUMNS: tuple[str, ...] = ("source_reader_capability", "source_assembly")


@dataclass(frozen=True)
class AdapterProjection:
    """How one Store Layout's builder manifest is serialised.

    ``columns`` is the ordered builder-manifest column set that layout's
    ``build-store.py`` adapter wrote, or ``None`` for ``canonical_columns``
    (the full lossless set plus the ancestry-proportion columns the data
    carries). ``registry_columns`` means the layout's builder reads the registry
    Analysis column names directly, so rows are serialised verbatim under their
    own column names. ``release_sourced_columns`` are canonical columns the
    projection replaces with release-level ``build.yaml`` values rather than the
    row's own.
    """

    layout: str
    columns: tuple[str, ...] | None
    registry_columns: bool = False
    release_sourced_columns: tuple[str, ...] = ()


#: The 17 columns ``gwas-ssf-hybrid/build-store.py`` wrote, in that adapter's
#: order: the Dense set minus the six Analytical Metadata columns
#: listed in the module docstring (issue #82). Kept as an explicit projection --
#: and regression-tested as such -- so a Hybrid manifest is byte-identical to
#: the adapter's, and so the omission is a recorded decision rather than drift.
#: This is legacy compatibility debt: switching live Hybrid builds to the
#: canonical lossless representation changes what an accepted Store contains
#: and is deferred to the catalogue-path/rebuild work (#104).
LEGACY_HYBRID_COLUMNS: tuple[str, ...] = (
    "trait_id", "file_path", "trait_name", "n", "stored_effect_scale",
    "original_sd_method", "original_sd", "assigned_ancestry",
    "trait_ontology_id", "trait_ontology_label",
    "license", "publication_doi", "publication_pmid", "consortium", "first_author",
    "source_reader_capability", "source_assembly",
)

ADAPTER_PROJECTIONS: dict[str, AdapterProjection] = {
    # Dense's adapter *is* the lossless canonical translation, plus the ancestry
    # proportions the release happens to carry.
    "dense": AdapterProjection("dense", None),
    # Hybrid shares Dense's builder manifest shape but not its adapter's column
    # set, and sources capability/assembly from build.yaml. This projection is
    # legacy compatibility debt (issue #82); the lossless alternative is
    # canonical_manifest(), and adopting it for live builds is #104's call.
    "hybrid": AdapterProjection(
        "hybrid", LEGACY_HYBRID_COLUMNS, release_sourced_columns=RELEASE_SOURCED_COLUMNS
    ),
    # The Ragged GWAS-SSF builder reads registry Analysis column names directly.
    "ragged": AdapterProjection("ragged", None, registry_columns=True),
}


@dataclass
class BuilderManifest:
    """One layout's builder manifest, ready to serialise as TSV."""

    layout: str
    fieldnames: list[str]
    rows: list[dict[str, str]]


# ---------------------------------------------------------------------------
# Registry rows -> canonical builder-manifest rows
# ---------------------------------------------------------------------------


def buildable_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """The release's Analyses that belong in the build, in registry order.

    ``exclude_from_build=true`` marks an Analysis the release deliberately does
    not build (``CONTEXT.md``'s Inclusion Reason carries why). Every layout's
    current adapter omits these rows, and so must every projection here: a
    manifest that kept one would build a Store with an Analysis the release says
    it does not have.
    """
    return [dict(row) for row in rows if row.get("exclude_from_build") != "true"]


def ancestry_proportion_columns(rows: Iterable[Mapping[str, str]]) -> list[str]:
    """The ``ancestry_prop_<population>`` columns this data carries, sorted.

    Discovered from the rows rather than hardcoded, so a release gaining (or
    losing) an ancestry group changes the manifest without a code change.
    """
    return sorted({
        column
        for row in rows
        for column in row
        if column is not None and column.startswith(ANCESTRY_PROPORTION_PREFIX)
    })


def canonical_columns(rows: Sequence[Mapping[str, str]]) -> list[str]:
    """The lossless builder-manifest columns for these rows."""
    return [*CANONICAL_COLUMNS, *ancestry_proportion_columns(rows)]


def canonical_row(
    row: Mapping[str, str], ancestry_columns: Sequence[str] = ()
) -> dict[str, str]:
    """Translate one registry Analysis row into canonical builder-manifest columns.

    Values are copied verbatim; nothing is inferred. A column the row does not
    carry (or carries blank) stays blank, because only the manifest producer
    knows it.
    """
    missing = [column for column in REQUIRED_REGISTRY_COLUMNS if column not in row]
    if missing:
        raise KeyError(
            f"analysis row is missing required registry column(s): {', '.join(missing)}"
        )
    translated = {
        column: row.get(source, "") for column, source in CANONICAL_COLUMN_SOURCE.items()
    }
    translated.update({column: row.get(column, "") for column in ancestry_columns})
    return translated


def canonical_manifest(
    rows: Iterable[Mapping[str, str]],
    *,
    release_reader_capability: str = "",
    release_source_assembly: str = "",
) -> BuilderManifest:
    """The lossless builder manifest for a release's Analyses.

    This is the representation a caller should use when it needs every
    interpretation-bearing column -- in particular the six the legacy Hybrid
    projection omits (issue #82). ``release_reader_capability`` /
    ``release_source_assembly`` fill in only the rows that carry no per-row value
    of their own, so a row-level declaration always wins; pass them to describe a
    release whose Analyses have no ``source_reader_capability`` column.
    """
    buildable = buildable_rows(rows)
    ancestry_columns = ancestry_proportion_columns(buildable)
    canonical = [canonical_row(row, ancestry_columns) for row in buildable]
    for translated in canonical:
        if not translated["source_reader_capability"]:
            translated["source_reader_capability"] = release_reader_capability
        if not translated["source_assembly"]:
            translated["source_assembly"] = release_source_assembly
    return BuilderManifest("canonical", canonical_columns(buildable), canonical)


# ---------------------------------------------------------------------------
# Canonical rows -> one layout's adapter-compatible manifest
# ---------------------------------------------------------------------------


def _registry_columns(rows: Iterable[Mapping[str, str]]) -> list[str]:
    """The registry Analysis column names, in the order this release carries them.

    Read from the release's own table (including rows the build excludes) so the
    manifest header is the release's header: the Ragged builder reads these names
    directly, so renaming or reordering them would change its input.
    """
    columns: list[str] = []
    for row in rows:
        for column in row:
            if column is not None and column not in columns:
                columns.append(column)
    return columns


def builder_manifest(
    rows: Iterable[Mapping[str, str]],
    *,
    layout: str,
    release_reader_capability: str = "",
    release_source_assembly: str = "",
) -> BuilderManifest:
    """The adapter-compatible builder manifest for one Store Layout.

    ``layout`` is one of ``dense``, ``hybrid`` or ``ragged`` (see
    ``ADAPTER_PROJECTIONS``). The result is byte-identical to what that layout's
    retired ``build-store.py`` adapter wrote for the same ``analyses.tsv``.

    ``rows`` may be any iterable, including a single-use one (a generator or a
    ``csv.DictReader``). The Ragged projection needs the release's full table
    twice -- once for buildable rows and once for its registry column names --
    so it is materialised once here, at the API boundary, before either read.
    """
    try:
        projection = ADAPTER_PROJECTIONS[layout]
    except KeyError:
        raise ValueError(
            f"unknown Store Layout {layout!r}; expected one of "
            f"{', '.join(sorted(ADAPTER_PROJECTIONS))}"
        ) from None

    rows = list(rows)
    buildable = buildable_rows(rows)
    if projection.registry_columns:
        return BuilderManifest(
            projection.layout, _registry_columns(rows), buildable
        )

    ancestry_columns = ancestry_proportion_columns(buildable)
    canonical = [canonical_row(row, ancestry_columns) for row in buildable]
    release_values = {
        "source_reader_capability": release_reader_capability,
        "source_assembly": release_source_assembly,
    }
    for translated in canonical:
        for column in projection.release_sourced_columns:
            translated[column] = release_values[column]

    columns = (
        canonical_columns(buildable) if projection.columns is None else list(projection.columns)
    )
    return BuilderManifest(
        projection.layout, columns, [{column: row[column] for column in columns} for row in canonical]
    )


def write_builder_manifest(
    rows: Iterable[Mapping[str, str]],
    out_path: str | Path,
    *,
    layout: str,
    release_reader_capability: str = "",
    release_source_assembly: str = "",
) -> BuilderManifest:
    """Write one layout's adapter-compatible builder manifest TSV.

    Same tab-separated, LF-terminated, UTF-8 shape the ``build-store.py``
    adapters wrote into a temporary file before handing it to OpenGWASDB.
    """
    manifest = builder_manifest(
        rows,
        layout=layout,
        release_reader_capability=release_reader_capability,
        release_source_assembly=release_source_assembly,
    )
    with Path(out_path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, delimiter="\t", fieldnames=manifest.fieldnames, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(manifest.rows)
    return manifest
