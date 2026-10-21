#!/usr/bin/env python3
"""FinnGen metadata adapter: the R13 endpoint manifest -> Source Metadata Table (#288, ADR 0036).

FinnGen publishes one manifest row per endpoint in
``finngen_R13_manifest.tsv`` (``phenocode``, ``phenotype``, ``category``,
``num_cases``, ``num_controls``, ``path_https``, ...). This adapter converts it
into the canonical Source Metadata Table the shared freeze reads
(``manifest_generation.freeze``), so a later FinnGen round in a readable format
needs only a new Column Map and release config: the selection, the resolution,
the release policy and the rendering are the pipeline's.

The endpoint classification is the one the R path's metadata resolver applied
(``resources/generators/lib/metadata_resolvers/finngen_manifest.R``, retired
here), and it is deliberately strict: an endpoint the manifest cannot describe
honestly fails the whole adapter instead of shrinking the release silently.

- ``category`` is ``Quantitative endpoints`` (compared trimmed and
  case-insensitively, as the R resolver did): the endpoint is
  ``quantitative``, and its ``num_cases`` is the total sample size -- the
  inverse-rank-normalised traits are standardised over the whole cohort. Any
  ``num_controls`` upstream reports for such a row is ignored, never carried as
  a case count: the R resolver did the same, and a zero count where counts do
  not apply is the absence-vs-zero confusion ``bundle.check()`` refuses (#133).
- any other category with a positive ``num_cases`` **and** a positive
  ``num_controls``: ``case-control``, with both counts and their sum as the
  total sample size.
- anything else -- a quantitative endpoint with no positive ``num_cases``, a
  case-control endpoint with a zero count, a non-numeric count -- is refused by
  phenocode, with the reason.

No effect scale is declared: the manifest has no such column, and the release
config's method tiers decide ``stored_effect_scale``, ``original_effect_scale``,
``original_sd_method`` and ``sample_size_kind`` (the quantitative IRN endpoints
are declared standardised, so their tier says ``declared_standardised``).
Collection-wide facts -- the GRCh38 build, the FinnGen public-data licence and
the FinnGen consortium -- are the release config's defaults, not per-Analysis
source metadata (#287).

The Source Ancestry Label is FinnGen's ``Finnish`` for every endpoint, and the
super-population is the tracked map's translation of it
(``source_ancestry.load_source_label_translation``); an untranslated label is
refused by name rather than left blank. Padding is trimmed, as the GWAS
Catalog adapter trims it, and a value the canonical loader refuses removes the
written table so the adapter never leaves one behind for freeze.

Usage:
    python3 -m manifest_generation.providers.finngen.source_metadata_adapter \\
        --manifest /data/opengwasdb/raw/finngen-r13/releases/r13-full/manifest/finngen_R13_manifest.tsv \\
        --out <source-metadata.tsv>
"""

from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence

from manifest_generation import REPO_ROOT
from manifest_generation.connector_tsv import raise_problems, write_connector
from manifest_generation.source_ancestry import (
    SourceLabelTranslation,
    UnknownSourceAncestryLabel,
    load_source_label_translation,
)
from manifest_generation.source_inventory import InventoryError
from manifest_generation.source_metadata_table import (
    SOURCE_METADATA_TABLE,
    load_source_metadata_table,
)

#: The provider-manifest columns the adapter reads; any other column is ignored.
ADAPTER_INPUT_COLUMNS: tuple[str, ...] = (
    "phenocode",
    "phenotype",
    "category",
    "num_cases",
    "num_controls",
)

#: The canonical columns the adapter writes.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "source_analysis_id",
    "source_trait_label",
    "source_trait_id",
    "source_ancestry_label",
    "source_superpopulation",
    "study_design",
    "sample_size",
    "n_cases",
    "n_controls",
)

#: The ``category`` the manifest gives its inverse-rank-normalised endpoints.
QUANTITATIVE_CATEGORY = "Quantitative endpoints"

#: FinnGen's Source Ancestry Label for every endpoint; the tracked map holds the code.
FINNGEN_SOURCE_ANCESTRY_LABEL = "Finnish"


class AdapterError(InventoryError):
    """The provider manifest cannot be converted into a valid Source Metadata Table."""


def convert_manifest(
    manifest: Path, out: Path, *, repo_root: Path = REPO_ROOT
) -> None:
    """Convert ``manifest`` into a canonical Source Metadata Table at ``out``, then load it back.

    Loading the written table through the canonical loader is the gate: a
    table it refuses is removed, so the adapter never leaves one behind for the
    shared freeze.
    """
    translation = load_source_label_translation(repo_root)
    manifest, out = Path(manifest), Path(out)
    if not manifest.is_file():
        raise AdapterError(f"FinnGen endpoint manifest not found: {manifest}")
    rows: list[dict[str, str]] = []
    problems: list[str] = []
    with manifest.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        missing = [
            column for column in ADAPTER_INPUT_COLUMNS if column not in (reader.fieldnames or [])
        ]
        if missing:
            raise AdapterError(
                f"FinnGen endpoint manifest {manifest} is missing column(s): {', '.join(missing)}"
            )
        for line_number, raw in enumerate(reader, start=2):
            row, row_problems = _convert_row(raw, translation)
            problems.extend(
                f"line {line_number} ({row['source_analysis_id'] or 'no phenocode'}): {problem}"
                for problem in row_problems
            )
            rows.append(row)
    if problems:
        raise_problems(AdapterError, f"FinnGen endpoint manifest {manifest}", problems)
    write_connector(out, SOURCE_METADATA_TABLE, OUTPUT_COLUMNS, rows)
    try:
        load_source_metadata_table(out)
    except InventoryError:
        out.unlink()
        raise


def _convert_row(
    raw: Mapping[str, str | None], translation: SourceLabelTranslation
) -> tuple[dict[str, str], list[str]]:
    def text(column: str) -> str:
        return (raw.get(column) or "").strip()

    problems: list[str] = []
    phenocode = text("phenocode")
    category = text("category")
    # The R resolver compared the category case-insensitively after trimming.
    quantitative = category.lower() == QUANTITATIVE_CATEGORY.lower()
    n_cases, cases_problem = _count("num_cases", text("num_cases"), positive=True)
    # A total N of 0 is the source saying "not resolved"; a count of 0 controls
    # is a real count on a case-control row and is refused below only when the
    # endpoint has no controls at all.
    n_controls, controls_problem = _count("num_controls", text("num_controls"), positive=False)
    if cases_problem:
        problems.append(cases_problem)
    if controls_problem:
        problems.append(controls_problem)

    study_design = ""
    sample_size = ""
    if not problems:
        if quantitative:
            if n_cases is None:
                problems.append(
                    "a quantitative endpoint (category 'Quantitative endpoints') needs a positive "
                    f"num_cases as its total N; got {text('num_cases') or 'empty'!r}"
                )
            else:
                # The IRN traits are standardised over the whole cohort: num_cases
                # is the total, and any upstream num_controls is not a case count.
                study_design, sample_size = "quantitative", _render(n_cases)
        elif n_cases is not None and n_controls is not None and n_controls > 0:
            study_design = "case-control"
            sample_size = _render(n_cases + n_controls)
        else:
            problems.append(
                "an endpoint that is not a quantitative endpoint needs positive num_cases and "
                f"num_controls; got num_cases={text('num_cases') or 'empty'!r}, "
                f"num_controls={text('num_controls') or 'empty'!r}"
            )

    ancestry_label = FINNGEN_SOURCE_ANCESTRY_LABEL
    try:
        superpopulation = translation.superpopulation(ancestry_label)
    except UnknownSourceAncestryLabel as exc:
        problems.append(str(exc))
        superpopulation = ""

    row = {
        "source_analysis_id": phenocode,
        "source_trait_label": text("phenotype"),
        "source_trait_id": phenocode,
        "source_ancestry_label": ancestry_label,
        "source_superpopulation": superpopulation,
        "study_design": study_design,
        "sample_size": sample_size,
        "n_cases": _render(n_cases) if study_design == "case-control" else "",
        "n_controls": _render(n_controls) if study_design == "case-control" else "",
    }
    return row, problems


def _count(column: str, value: str, *, positive: bool) -> tuple[Decimal | None, str]:
    """``value`` as a non-negative count (a positive one when ``positive``), or why not."""
    if not value:
        return None, ""
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None, f"{column} {value!r} is not a number"
    if not number.is_finite() or number < 0:
        return None, f"{column} {value!r} is not a non-negative count"
    if positive and number == 0:
        return None, ""
    return number, ""


def _render(number: Decimal) -> str:
    """A count as the canonical table spells it: an integer when it is one."""
    return str(int(number)) if number == number.to_integral_value() else format(number, "f")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--manifest", required=True, type=Path, help="FinnGen's finngen_R13_manifest.tsv"
    )
    parser.add_argument("--out", required=True, type=Path, help="the Source Metadata Table to write")
    args = parser.parse_args(argv)
    try:
        convert_manifest(args.manifest, args.out)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Source Metadata Table {args.out} written from {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
