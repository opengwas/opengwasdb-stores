"""`summary.html`: the Quarto summary report, rendered into each new version (#325).

The report source is `docs/reports/analysis-catalog-summary.qmd` (ADR 0038). It
reads the version's own `catalog.sqlite` and `changes.tsv` and nothing else.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ogstores import paths

SOURCE = paths.REPO_ROOT / "docs" / "reports" / "analysis-catalog-summary.qmd"
SUMMARY_HTML = "summary.html"


def render(version_dir: Path) -> None:
    """Render the summary of the catalog in `version_dir` into `version_dir/summary.html`."""
    from analysis_catalog.build import CatalogError

    quarto = shutil.which("quarto")
    if quarto is None:
        raise CatalogError(
            "quarto is not on PATH, so the summary report cannot be rendered; "
            "run the catalog in an environment with the reporting feature: pixi run -e dev catalog"
        )
    # Rendered from a scratch copy: Quarto's `--output-dir` breaks self-contained
    # output, and a render in place would leave its files in the checkout.
    with tempfile.TemporaryDirectory(prefix="catalog_summary_") as scratch:
        source = Path(scratch) / SOURCE.name
        shutil.copyfile(SOURCE, source)
        result = subprocess.run(
            [quarto, "render", source.name, "--to", "html", "--output", SUMMARY_HTML,
             "-P", f"catalog_dir:{version_dir.resolve()}"],
            capture_output=True, text=True, cwd=scratch,
        )
        rendered = Path(scratch) / SUMMARY_HTML
        if result.returncode != 0 or not rendered.is_file():
            raise CatalogError(f"the summary report failed to render:\n{result.stdout}{result.stderr}".rstrip())
        shutil.copyfile(rendered, version_dir / SUMMARY_HTML)
