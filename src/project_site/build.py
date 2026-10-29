"""Render the site: stage both repositories' docs, rewrite their links, render, check (#388).

`pixi run site` renders the checkout you are in; `site-publish` renders a clean
checkout of a ref through `project_site.publish`. Both come through
`build_site`, so what you preview is what is published.

The render reads a mirror of `docs/` under the work root, never `docs/` itself:
that is where links are rewritten to GitHub and where a plain ```` ```mermaid ````
fence is spelled the way Quarto renders it, without editing a source file. The
engine's docs are staged from the revision `pixi.lock` pins before the mirror is
made, so the engine section describes the runtime the registry imports.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from project_site import paths, staging, urlcheck
from project_site.staging import LinkReport

ENGINE_SECTION = "opengwasdb"

# What the site serves besides the pages Quarto renders. Quarto copies these
# from `project.resources` in `_quarto.yml`; the list lives there because it is
# what makes the output complete, not what the build produces.
SECTION_TITLES = {
    ".": "Docs",
    "adr": "Decisions (ADRs)",
    "spec": "Specifications",
    "benchmark-output": "Benchmarks and reports",
    "examples": "Examples",
    "reviews": "Reviews",
}


@dataclass
class BuildReport:
    """One render, with what the summary prints."""

    repo: Path
    commit: str
    engine_commit: str
    staging: Path
    output: Path
    seconds: float
    bytes: int
    pages: int
    links: LinkReport
    served: list[urlcheck.PathStatus]


def _quarto_render(staging: Path, output: Path) -> None:
    if output.exists():
        import shutil

        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["quarto", "render", str(staging), "--output-dir", str(output)],
        check=True,
    )


def _write_raw_pages(output: Path, raw_pages: list[tuple[str, str]]) -> None:
    """Publish the Markdown of a page Quarto had to read as `.qmd`.

    Every path the site serves today stays, and a published `.md` page is one of
    them: the page renders to `.html` and its source stays readable at `.md`.
    """
    for page, text in raw_pages:
        destination = output / page
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")


def _first_heading(markdown: Path) -> str | None:
    for line in markdown.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _html_title(page: Path) -> str | None:
    text = page.read_text(encoding="utf-8", errors="replace")
    start = text.lower().find("<title>")
    if start == -1:
        return None
    end = text.lower().find("</title>", start)
    if end == -1:
        return None
    return " ".join(text[start + len("<title>"):end].split()) or None


def write_engine_index(dest: Path, *, revision: str, repository_url: str) -> Path:
    """The engine section's own index: every staged page, grouped by directory.

    opengwasdb's hand-built `docs/index.html` links at `.md` sources and is not
    copied; this page is generated from what is actually staged.
    """
    pages = sorted(
        path.relative_to(dest).as_posix()
        for path in dest.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".html"}
    )
    groups: dict[str, list[str]] = {}
    for page in pages:
        parent = page.rsplit("/", 1)[0] if "/" in page else "."
        groups.setdefault(parent, []).append(page)
    ordered = sorted(groups, key=lambda name: (name != ".", name))

    lines = [
        "---",
        'title: "Engine (opengwasdb)"',
        "---",
        "",
        "opengwasdb's documentation, rendered from the revision this registry pins",
        f"([`{revision}`]({repository_url}/tree/{revision})). It describes the runtime",
        "the registry imports, so the engine section moves when that pin moves.",
        "",
    ]
    for group in ordered:
        lines += [f"## {SECTION_TITLES.get(group, group.replace('-', ' ').title())}", ""]
        for page in groups[group]:
            source = dest / page
            title = _html_title(source) if page.endswith(".html") else _first_heading(source)
            link = page.rsplit(".", 1)[0] + ".html"
            lines.append(f"- [{title or page}]({link})")
        lines.append("")
    index = dest / staging.ENGINE_INDEX
    index.write_text("\n".join(lines), encoding="utf-8")
    return index


class SiteCheckFailed(RuntimeError):
    """A render does not serve every path the site serves today, so it is not published."""


@dataclass
class Stage:
    """What a render reads: the commits it documents and the mirror it renders."""

    repo: Path
    commit: str
    engine_commit: str
    clone: Path
    mirror: Path


def stage_everything(repo: Path) -> Stage:
    """Stage the engine's docs, the glossary and the docs mirror."""
    repo = repo.resolve()
    commit = paths.commit_of(repo)
    pinned = paths.pinned_opengwasdb_revision(repo / "pixi.lock")
    clone = staging.ensure_engine_clone(paths.engine_clone(), pinned, url=paths.ENGINE_URL)
    engine_pages = staging.stage_docs(clone, pinned, paths.engine_staging(repo))
    if not engine_pages:
        raise SystemExit(f"no documentation staged from {clone} at {pinned}")
    # opengwasdb's own hand-built landing page is not copied: its links point at
    # `.md` sources, and the engine section's index is generated instead.
    (paths.engine_staging(repo) / "index.html").unlink(missing_ok=True)
    staging.stage_glossary(repo / "CONTEXT.md", paths.glossary_page(repo))
    write_engine_index(paths.engine_staging(repo), revision=pinned, repository_url=paths.ENGINE_URL)
    mirror = paths.staging_dir(repo)
    staging.mirror_docs(repo, mirror)
    return Stage(repo=repo, commit=commit, engine_commit=pinned, clone=clone, mirror=mirror)


def rewrite(stage: Stage) -> LinkReport:
    """Point the mirror's outbound links at GitHub and spell its Mermaid fences for Quarto."""
    return staging.rewrite_mirror(
        stage.mirror,
        registry=stage.repo,
        commit=stage.commit,
        registry_url=paths.REGISTRY_URL,
        engine=stage.clone,
        engine_commit=stage.engine_commit,
        engine_url=paths.ENGINE_URL,
        engine_prefix=f"{ENGINE_SECTION}/",
    )


def build_site(*, repo: Path | None = None, output: Path | None = None, check_urls: bool = True) -> BuildReport:
    """Render the site for `repo` into `output`, and check what the site serves today."""
    repo = Path(repo).resolve() if repo is not None else paths.repo_root()
    output = Path(output).resolve() if output is not None else paths.output_dir(repo)
    stage = stage_everything(repo)
    links = rewrite(stage)

    started = time.monotonic()
    _quarto_render(stage.mirror, output)
    _write_raw_pages(output, links.raw_pages)
    seconds = time.monotonic() - started

    files = [path for path in output.rglob("*") if path.is_file()]
    served: list[urlcheck.PathStatus] = []
    if check_urls:
        try:
            served_paths = urlcheck.paths_from_git(repo, "origin/main")
        except subprocess.CalledProcessError:
            print("warning: origin/main is not available; the served-path check was skipped")
        else:
            served = urlcheck.check_paths(served_paths, output)

    return BuildReport(
        repo=repo,
        commit=stage.commit,
        engine_commit=stage.engine_commit,
        staging=stage.mirror,
        output=output,
        seconds=seconds,
        bytes=sum(path.stat().st_size for path in files),
        pages=sum(1 for path in files if path.suffix.lower() == ".html"),
        links=links,
        served=served,
    )


def render_checkout(source_commit: str, checkout: Path, output: Path) -> Path:
    """The renderer `project_site.publish` calls: the ref's own docs, rendered cleanly."""
    report = build_site(repo=checkout, output=output)
    print(f"rendered {report.pages} pages from {source_commit} in {report.seconds:.1f}s")
    missing = [status.path for status in report.served if status.state == "missing"]
    if missing:
        raise SiteCheckFailed(
            f"the render of {source_commit} does not serve {len(missing)} path(s) the site serves today: "
            f"{', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}"
        )
    return report.output


def report_summary(report: BuildReport) -> str:
    lines = [
        f"rendered  {report.pages} pages, {report.bytes / 1e6:.1f} MB, {report.seconds:.1f}s",
        f"output    {report.output}",
        f"commit    {report.commit} (engine {report.engine_commit})",
        f"links     {report.links.rewritten} rewritten to GitHub on {report.links.pages} pages",
    ]
    if report.links.missing:
        per_page: dict[str, int] = {}
        for page, _target, _repo_path in report.links.missing:
            per_page[page] = per_page.get(page, 0) + 1
        lines.append(
            f"          {len(report.links.missing)} of them name a path the commit does not have,"
            " mostly in the historical audit documents:"
        )
        lines.append(
            "            "
            + ", ".join(f"{page} {count}" for page, count in sorted(per_page.items(), key=lambda item: -item[1]))
        )
    if report.served:
        lines.append(f"served    {urlcheck.summarise(report.served)}")
        for status in report.served:
            if status.state == "missing":
                lines.append(f"            missing {status.path}")
    return "\n".join(lines)


# What a re-stage leaves alone: the build's own output and the engine's staged
# docs (regenerated anyway), Quarto's cache, and the report sources, which are
# rendered by `pixi run docs` rather than by the site.
WATCH_IGNORE = ("reports/", "opengwasdb/", "glossary.md", ".quarto/", "_site/")


def _snapshot(repo: Path) -> dict[str, int]:
    docs = repo / "docs"
    stamps: dict[str, int] = {}
    for path in docs.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(docs).as_posix()
        if relative.startswith(WATCH_IGNORE):
            continue
        stamps[relative] = path.stat().st_mtime_ns
    return stamps


def _restage_on_change(repo: Path, stop: "threading.Event") -> None:
    """Keep the mirror current while the preview serves it: a source edit is re-staged."""
    previous = _snapshot(repo)
    while not stop.wait(1.0):
        current = _snapshot(repo)
        if current == previous:
            continue
        previous = current
        try:
            rewrite(stage_everything(repo))
            print("restaged the docs mirror")
        except Exception as error:  # a half-written file is not a reason to stop the preview
            print(f"re-stage failed: {error}", file=sys.stderr)


def preview(repo: Path | None = None) -> int:
    """Stage, then serve the staging tree with Quarto's live-reloading preview.

    Quarto watches the staging tree, and a thread watches the sources and
    re-stages on change, so editing `docs/` re-renders the page you are looking
    at without restarting the command.
    """
    repo = Path(repo).resolve() if repo is not None else paths.repo_root()
    rewrite(stage_everything(repo))
    stop = threading.Event()
    watcher = threading.Thread(target=_restage_on_change, args=(repo, stop), daemon=True)
    watcher.start()
    process = subprocess.Popen(["quarto", "preview", str(paths.staging_dir(repo))])
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
    finally:
        stop.set()
    return process.returncode or 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=paths.repo_root(), help="checkout whose docs/ is rendered")
    parser.add_argument("--output", type=Path, default=None, help="output directory (never under /home)")
    parser.add_argument("--preview", action="store_true", help="serve the staged tree with live reload")
    parser.add_argument("--no-url-check", action="store_true", help="skip the served-path check")
    args = parser.parse_args(argv)

    if args.preview:
        return preview(args.repo)
    report = build_site(repo=args.repo, output=args.output, check_urls=not args.no_url_check)
    print(report_summary(report))
    return 1 if any(status.state == "missing" for status in report.served) else 0


if __name__ == "__main__":
    sys.exit(main())
