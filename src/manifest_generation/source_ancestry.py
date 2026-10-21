"""The tracked Source Ancestry Label -> super-population map, read one way (#133, #283).

``resources/reference-resources/ukb-ancestry-mixture-hg38/source_label_map.tsv``
translates a free-text Source Ancestry Label (``European``) to one of the
ancestry mixture's super-population codes (``EUR``). R reads it through
``ancestry.R``; every Python reader reads it here, so the candidate workflow,
the ancestry-assignment stage and the GWAS Catalog metadata adapter cannot
parse it three ways.

The map lists only labels precise enough to translate. Its companion,
``source_label_imprecise.tsv``, lists the labels left out on purpose --
``Multiple/Mixed``, ``NR/Unknown``, ``Other``, ``Asian (unspecified)`` -- each
with its reason. :func:`load_source_label_translation` reads both: a mapped
label gives its code, a deliberately imprecise one gives blank, and any other
label is refused by name, so a new or misspelt label cannot pass as blank.
The map stays exactly as ``ancestry.R`` and the bundle tests read it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from manifest_generation import REPO_ROOT
from manifest_generation.source_inventory import PreflightConfigError
from ogstores.bundle import SUPERPOPULATIONS

#: Path (relative to the repository root) of the tracked map, as ``ancestry.R`` reads it.
SOURCE_LABEL_MAP_RELATIVE_PATH: str = (
    "resources/reference-resources/ukb-ancestry-mixture-hg38/source_label_map.tsv"
)

SOURCE_LABEL_MAP_COLUMNS: tuple[str, ...] = ("source_label", "super_population")

#: Path of the companion listing the labels the map leaves out on purpose.
SOURCE_LABEL_IMPRECISE_RELATIVE_PATH: str = (
    "resources/reference-resources/ukb-ancestry-mixture-hg38/source_label_imprecise.tsv"
)

SOURCE_LABEL_IMPRECISE_COLUMNS: tuple[str, ...] = ("source_label", "reason")


class UnknownSourceAncestryLabel(PreflightConfigError):
    """A Source Ancestry Label neither the map translates nor the imprecise list names."""


def load_source_label_map(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Every Source Ancestry Label the tracked map translates, with its super-population code.

    Refuses a missing file, a wrong header, an empty cell, a label listed
    twice, and a code outside ``ogstores.bundle.SUPERPOPULATIONS``.
    """
    path = Path(repo_root) / SOURCE_LABEL_MAP_RELATIVE_PATH
    if not path.is_file():
        raise PreflightConfigError(f"tracked Source Ancestry Label map not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if tuple(reader.fieldnames or ()) != SOURCE_LABEL_MAP_COLUMNS:
            raise PreflightConfigError(
                f"{path} must have exactly the columns {', '.join(SOURCE_LABEL_MAP_COLUMNS)}"
            )
        mapping: dict[str, str] = {}
        for line_number, row in enumerate(reader, start=2):
            label = (row.get("source_label") or "").strip()
            code = (row.get("super_population") or "").strip()
            if not label or not code:
                raise PreflightConfigError(f"{path}:{line_number}: empty source_label or super_population")
            if label in mapping:
                raise PreflightConfigError(f"{path}:{line_number}: {label!r} is mapped twice")
            if code not in SUPERPOPULATIONS:
                raise PreflightConfigError(
                    f"{path} maps {label!r} to {code!r}, which is not a super-population code"
                )
            mapping[label] = code
    return mapping


def load_imprecise_source_labels(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """The labels the map leaves out on purpose, each with its reason.

    Refuses a missing file, a wrong header, an empty cell and a label listed
    twice.
    """
    path = Path(repo_root) / SOURCE_LABEL_IMPRECISE_RELATIVE_PATH
    if not path.is_file():
        raise PreflightConfigError(f"tracked imprecise Source Ancestry Label list not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if tuple(reader.fieldnames or ()) != SOURCE_LABEL_IMPRECISE_COLUMNS:
            raise PreflightConfigError(
                f"{path} must have exactly the columns {', '.join(SOURCE_LABEL_IMPRECISE_COLUMNS)}"
            )
        listed: dict[str, str] = {}
        for line_number, row in enumerate(reader, start=2):
            label = (row.get("source_label") or "").strip()
            reason = (row.get("reason") or "").strip()
            if not label or not reason:
                raise PreflightConfigError(f"{path}:{line_number}: empty source_label or reason")
            if label in listed:
                raise PreflightConfigError(f"{path}:{line_number}: {label!r} is listed twice")
            listed[label] = reason
    return listed


@dataclass(frozen=True)
class SourceLabelTranslation:
    """The map and its imprecise list together: every label is translated, deliberately blank, or refused."""

    mapped: Mapping[str, str]
    imprecise: Mapping[str, str]

    def superpopulation(self, label: str) -> str:
        """``label``'s super-population code; blank for an empty or deliberately imprecise label.

        Any other label raises :class:`UnknownSourceAncestryLabel`, naming it.
        """
        if not label or label in self.imprecise:
            return ""
        code = self.mapped.get(label)
        if code is None:
            raise UnknownSourceAncestryLabel(
                f"Source Ancestry Label {label!r} is neither in {SOURCE_LABEL_MAP_RELATIVE_PATH} nor in "
                f"{SOURCE_LABEL_IMPRECISE_RELATIVE_PATH}; map it or list it as imprecise, with a reason"
            )
        return code


def load_source_label_translation(repo_root: Path = REPO_ROOT) -> SourceLabelTranslation:
    """Both tracked files, refusing a label that is both mapped and listed as imprecise."""
    mapped = load_source_label_map(repo_root)
    imprecise = load_imprecise_source_labels(repo_root)
    both = sorted(set(mapped) & set(imprecise))
    if both:
        raise PreflightConfigError(
            f"Source Ancestry Label(s) both mapped and listed as imprecise: {', '.join(both)}"
        )
    return SourceLabelTranslation(mapped=mapped, imprecise=imprecise)
