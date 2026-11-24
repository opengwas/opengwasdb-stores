"""The two layouts the audit suites build a fixture Store in: format 0.2.x and 0.3.0.

`write_variant_axis` writes only what 0.3.0 carries (opengwasdb#467-#472): the
variable-length ALID sidecar, the position and `source_alid` sidecars, and for a Hybrid no
Variant Axis of the Dense Component's own. Hosted releases are 0.2.x until the rebuild
(opengwas/opengwasdb-stores#498), so the audit has to read both, and a fixture of the 0.2
shape is made here from the same writer. `write_fixed_width_alid_index` and
`write_variant_offsets` are the engine's own test helpers (`tests/format_0_2_3.py` at
opengwasdb 7446e750), which write what `write_variant_axis` wrote before #467.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from opengwasdb.store_reader.alid import ALID_DTYPE, is_indexable_alid
from opengwasdb.store_reader.alid_sidecar import ALID_SIDECAR_FILENAMES, DuplicateAlidError
from opengwasdb.store_reader.position_sidecar import POSITION_SIDECAR_FILENAMES
from opengwasdb.store_reader.source_alid_sidecar import SOURCE_ALID_SIDECAR_FILENAMES
from opengwasdb.store_reader.variant_axis import variant_alid_bytes_path, variant_alid_rows_path
from opengwasdb.variants.axis import write_variant_axis
from opengwasdb.variants.normalise import CanonicalVariant

LAYOUTS = ("0.2", "0.3")
#: The `format_version` a fixture manifest names in each layout.
FORMAT_VERSION = {"0.2": "0.2.0", "0.3": "0.3.0"}

#: What a 0.3.0 Hybrid's Dense Component does not carry (opengwasdb#472): the shared
#: axis serves it through `dense_to_shared.npy`.
DENSE_AXIS_FILES = (
    "variants.tsv.gz",
    "variants.tsv.gz.tbi",
    "variant_rsid_bytes.npy",
    "variant_rsid_rows.npy",
    *ALID_SIDECAR_FILENAMES,
    *POSITION_SIDECAR_FILENAMES,
    *SOURCE_ALID_SIDECAR_FILENAMES,
)


def write_fixed_width_alid_index(directory: Path, alids: Sequence[str]) -> None:
    """The 0.2 series' ALID index for `alids` (one per row): ALIDs of at most 32 bytes."""
    indexable = [(alid, i) for i, alid in enumerate(alids) if is_indexable_alid(alid)]
    alid_bytes = np.array([alid for alid, _ in indexable], dtype=ALID_DTYPE)
    row_indices = np.array([i for _, i in indexable], dtype="int32")
    order = np.argsort(alid_bytes, kind="stable")
    np.save(variant_alid_bytes_path(directory), alid_bytes[order])
    np.save(variant_alid_rows_path(directory), row_indices[order])


def write_variant_offsets(directory: Path) -> None:
    """`variant_offsets.npy`: each row's BGZF virtual offset in the Variant Table."""
    import pysam

    offsets: list[int] = []
    with pysam.BGZFile(str(directory / "variants.tsv.gz"), "r", None) as handle:
        while True:
            at = int(handle.tell())
            line = handle.readline()
            if not line:
                break
            if not line.startswith(b"#"):
                offsets.append(at)
    np.save(directory / "variant_offsets.npy", np.asarray(offsets, dtype=np.uint64))


def write_axis(
    directory: Path,
    variants: list[CanonicalVariant],
    rsids: Mapping[str, str],
    layout: str,
) -> None:
    """The Variant Table and its indexes in `directory`, in the layout's shape.

    A 0.3.0 axis refuses a repeated ALID, as the engine does. A 0.2 one can hold it, which
    is how a test reaches the audit's duplicate count: the table is written first, then
    the writer stops at the sidecar, and the fixed-width pair is written from the list.
    """
    if layout == "0.3":
        write_variant_axis(directory, variants, rsids)
        return
    try:
        write_variant_axis(directory, variants, rsids)
    except DuplicateAlidError:
        pass
    for name in (*ALID_SIDECAR_FILENAMES, *POSITION_SIDECAR_FILENAMES, *SOURCE_ALID_SIDECAR_FILENAMES):
        (directory / name).unlink(missing_ok=True)
    write_fixed_width_alid_index(directory, [variant.alid for variant in variants])
    write_variant_offsets(directory)


def write_dense_axis(
    dense: Path,
    variants: list[CanonicalVariant],
    rsids: Mapping[str, str],
    layout: str,
) -> None:
    """The Dense Component's own axis: a 0.2 release carries one, a 0.3.0 one carries none."""
    if layout == "0.2":
        write_axis(dense, variants, rsids, layout)


def restamp(manifest_path: Path, layout: str) -> None:
    """Name the layout's format in a fixture manifest, leaving every other byte."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["format_version"] = FORMAT_VERSION[layout]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def twin_for_each_layout(namespace: dict, cases: Sequence[type]) -> list[type]:
    """`cases` as they are (layout 0.3) and a twin of each at 0.2; returns every class.

    A case sets `LAYOUT`. The twin's name ends `OnFormat0_2`, so a failure says which.
    """
    out: list[type] = []
    for case in cases:
        case.LAYOUT = "0.3"
        twin = type(f"{case.__name__}OnFormat0_2", (case,), {"LAYOUT": "0.2", "__module__": case.__module__})
        namespace[twin.__name__] = twin
        out.extend([case, twin])
    return out
