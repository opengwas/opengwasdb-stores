"""`summary.html`: the Quarto summary report, rendered into each new version (#325).

The report source is `docs/reports/analysis-catalog-summary.qmd` (ADR 0038). It
reads the version's own `catalog.sqlite` and `changes.tsv` and nothing else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ogstores import paths

from analysis_catalog.errors import CatalogError

SOURCE = paths.REPO_ROOT / "docs" / "reports" / "analysis-catalog-summary.qmd"
SUMMARY_HTML = "summary.html"


def quarto_path() -> Path:
    """The Quarto of the active Pixi environment, or CatalogError.

    Only `$CONDA_PREFIX/bin/quarto`: a Quarto found on `PATH` may be another
    installation with other, unpinned R packages.
    """
    prefix = os.environ.get("CONDA_PREFIX")
    quarto = Path(prefix, "bin", "quarto") if prefix else None
    if quarto is None or not quarto.is_file():
        raise CatalogError(
            "the summary report needs Quarto from the Pixi environment that has the reporting "
            "feature (CONDA_PREFIX/bin/quarto), and none is active; run `pixi run catalog`"
        )
    return quarto


def render(version_dir: Path) -> None:
    """Render the summary of the catalog in `version_dir` into `version_dir/summary.html`."""
    quarto = quarto_path()
    # Rendered from a scratch copy: Quarto's `--output-dir` breaks self-contained
    # output, and a render in place would leave its files in the checkout.
    with tempfile.TemporaryDirectory(prefix="catalog_summary_") as scratch:
        source = Path(scratch) / SOURCE.name
        shutil.copyfile(SOURCE, source)
        result = subprocess.run(
            [str(quarto), "render", source.name, "--to", "html", "--output", SUMMARY_HTML,
             "-P", f"catalog_dir:{version_dir.resolve()}"],
            capture_output=True, text=True, cwd=scratch,
        )
        rendered = Path(scratch) / SUMMARY_HTML
        if result.returncode != 0 or not rendered.is_file():
            raise CatalogError(f"the summary report failed to render:\n{result.stdout}{result.stderr}".rstrip())
        shutil.copyfile(rendered, version_dir / SUMMARY_HTML)
