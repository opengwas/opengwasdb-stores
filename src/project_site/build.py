"""Render the site: stage both repositories' docs, rewrite their links, render, check (#388).

`pixi run site` renders the checkout you are in; `site-publish` renders a clean
checkout of a ref through `project_site.publish`. Both come through
`build_site`, so what you preview is what is published.

The render reads a mirror of `docs/` under the work root, never `docs/` itself:
that is where links are rewritten to GitHub, where a plain ```` ```mermaid ````
fence is spelled the way Quarto renders it, and where the engine's docs and the
glossary are staged into. Nothing is written into the checkout, so two renders of
different checkouts, or of one checkout into two outputs, never collide.

The engine's docs come from the revision `pixi.lock` pins, so the engine section
describes the runtime the registry imports.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from project_site import paths, staging, urlcheck
from project_site.collect import __main__ as site_data
from project_site.collect import common as collect_common
from project_site.pages import COMMIT_ENV_VAR
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


# What a directory that already holds a render has. A render replaces a previous
# render, and nothing else: anything else at the output path is not ours to delete.
RENDER_MARKERS = frozenset({"index.html", "search.json", "site_libs"})


def collect_data(mirror: Path, repo: Path) -> list[dict]:
    """Collect the site's data into the render's own directory (#391).

    The pages are rendered from the data of the checkout being rendered, so
    `pixi run site` and a publish of a clean worktree both collect there, and a
    source that cannot be read is a notice on a page rather than a page that
    says nothing else. `OPENGWASDB_SITE_DATA` names a directory that was
    collected already, and then nothing is collected here.
    """
    if os.environ.get(collect_common.DATA_DIR_ENV_VAR, "").strip():
        return []
    return site_data.run(
        mirror / "_data",
        registry=repo / "stores",
        epics_dir=repo / "docs" / "_epics",
        yardstick_dir=repo / "docs" / "_yardstick",
    )


def render_env(commit: str) -> dict[str, str]:
    """The environment a render runs in: it names the commit the pages link to (#391)."""
    return {**os.environ, COMMIT_ENV_VAR: commit}


def _quarto_render(staging: Path, output: Path, *, commit: str) -> None:
    if output.exists():
        import shutil

        held = {path.name for path in output.iterdir()}
        if held and not held & RENDER_MARKERS:
            raise SystemExit(
                f"{output} exists and does not hold a previous render; refusing to replace it"
            )
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["quarto", "render", str(staging), "--output-dir", str(output)],
        check=True,
        env=render_env(commit),
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


def stage_everything(
    repo: Path,
    *,
    clone: Path | None = None,
    revision: str | None = None,
    stage_dir: Path | None = None,
) -> Stage:
    """Stage the mirror: this repository's docs, the engine's docs and the glossary.

    `clone` and `revision` name the engine repository and commit; left out, they
    come from the revision `pixi.lock` pins and the clone under the work root.
    `stage_dir` is where the mirror is built; left out, it sits beside the output
    under the work root. A publish gives its own, because the checkout it renders
    is a worktree that must stay clean for `git worktree remove` to accept it.
    """
    repo = repo.resolve()
    commit = paths.commit_of(repo)
    pinned = revision or paths.pinned_opengwasdb_revision(repo / "pixi.lock")
    clone = Path(clone) if clone is not None else staging.ensure_engine_clone(
        paths.engine_clone(), pinned, url=paths.ENGINE_URL
    )
    mirror = Path(stage_dir) if stage_dir is not None else paths.staging_dir(repo)
    staging.mirror_docs(repo, mirror)
    engine_section = mirror / ENGINE_SECTION
    engine_pages = staging.stage_docs(clone, pinned, engine_section)
    if not engine_pages:
        raise SystemExit(f"no documentation staged from {clone} at {pinned}")
    # opengwasdb's own hand-built landing page is not copied: its links point at
    # `.md` sources, and the engine section's index is generated instead.
    (engine_section / "index.html").unlink(missing_ok=True)
    staging.stage_glossary(repo / "CONTEXT.md", mirror / "glossary.md")
    write_engine_index(engine_section, revision=pinned, repository_url=paths.ENGINE_URL)
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


def served_paths(repo: Path) -> list[str]:
    """The paths the published site serves today, from `origin/main`.

    The gate this feeds fails closed: if that revision cannot be read, there is
    no list to check against, and a render that cannot be checked is not one to
    publish. Silence here would let a publish drop real, read URLs unnoticed.
    """
    try:
        return urlcheck.paths_from_git(repo, "origin/main")
    except subprocess.CalledProcessError as error:
        raise SiteCheckFailed(
            f"cannot read the paths served today from origin/main in {repo}: {error.stderr.strip()}"
        ) from error


def build_site(
    *,
    repo: Path | None = None,
    output: Path | None = None,
    check_urls: bool = True,
    stage_dir: Path | None = None,
) -> BuildReport:
    """Render the site for `repo` into `output`, and check what the site serves today."""
    repo = Path(repo).resolve() if repo is not None else paths.repo_root()
    output = Path(output).resolve() if output is not None else paths.output_dir(repo)
    stage = stage_everything(repo, stage_dir=stage_dir)
    collect_data(stage.mirror, repo)
    links = rewrite(stage)

    started = time.monotonic()
    _quarto_render(stage.mirror, output, commit=stage.commit)
    _write_raw_pages(output, links.raw_pages)
    seconds = time.monotonic() - started

    files = [path for path in output.rglob("*") if path.is_file()]
    served = urlcheck.check_paths(served_paths(repo), output) if check_urls else []

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
    """The renderer `project_site.publish` calls: the ref's own docs, rendered cleanly.

    The mirror sits beside the output, not under the work root's name for this
    checkout: the checkout is a scratch worktree, and a render that wrote inside it
    would leave it dirty and unremovable.
    """
    report = build_site(repo=checkout, output=output, stage_dir=output.with_name(f"{output.name}-stage"))
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
    if report.links.unparsed:
        lines.append(
            f"          {len(report.links.unparsed)} link destination(s) could not be read, so were left alone:"
        )
        for page, target in report.links.unparsed:
            lines.append(f"            {page}: {target}")
    if report.served:
        lines.append(f"served    {urlcheck.summarise(report.served)}")
        for status in report.served:
            if status.state == "missing":
                lines.append(f"            missing {status.path}")
    return "\n".join(lines)


# What a re-stage leaves alone: Quarto's cache and the site's own output, and the
# report sources, which `pixi run docs` renders rather than the site.
WATCH_IGNORE = ("reports/", ".quarto/", "_site/")

# The inputs outside `docs/`: `CONTEXT.md` is the glossary page, and `pixi.lock`
# pins the engine revision the engine section is staged from.
WATCH_ROOT = ("CONTEXT.md", "pixi.lock")

# Inside the served tree: `quarto preview`'s own output and cache. A sync must
# never copy into them or delete them.
SERVED_OWN = ("_site", ".quarto")


def watched_snapshot(repo: Path) -> dict[str, int]:
    """The modification times of every input a render reads from `repo`.

    A render's inputs are not only `docs/`: the glossary comes from `CONTEXT.md`
    and the engine section from the revision `pixi.lock` pins. Those staged
    copies are generated, so watching them would only see the build's own work.
    """
    docs = repo / "docs"
    stamps: dict[str, int] = {}
    for relative in WATCH_ROOT:
        path = repo / relative
        if path.is_file():
            stamps[relative] = path.stat().st_mtime_ns
    for path in docs.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(docs).as_posix()
        if relative.startswith(WATCH_IGNORE):
            continue
        if relative == "glossary.md" or relative.startswith(f"{ENGINE_SECTION}/"):
            continue
        stamps[f"docs/{relative}"] = path.stat().st_mtime_ns
    return stamps


def _sync_tree(source: Path, served: Path) -> int:
    """Make `served` match `source` file by file, atomically; return how many changed.

    Quarto watches `served` and re-renders what changed. A file is replaced with
    a rename, so a read never sees half of one, and only a changed file is
    written: an untouched page keeps its timestamp and is not re-rendered.
    """
    changed = 0
    wanted: set[str] = set()
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if relative.parts and relative.parts[0] in SERVED_OWN:
            continue
        data = path.read_bytes()
        wanted.add(relative.as_posix())
        target = served / relative
        mode = path.stat().st_mode & 0o777
        if target.is_file() and target.read_bytes() == data:
            # The preview tree is what Quarto runs the project's `pre-render`
            # hook from (`docs/pre-render.sh`, #391), so the mirror's mode is
            # part of what `served` matches: a file written fresh is 0644.
            if target.stat().st_mode & 0o777 != mode:
                os.chmod(target, mode)
                changed += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.staging")
        temporary.write_bytes(data)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        changed += 1
    for path in served.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(served)
        if relative.parts and relative.parts[0] in SERVED_OWN:
            continue
        if relative.as_posix() not in wanted:
            path.unlink()
            changed += 1
    return changed


def _restage_on_change(repo: Path, served: Path, stop: "threading.Event") -> None:
    """Keep the served tree current while the preview runs: a source edit is re-staged."""
    previous = watched_snapshot(repo)
    while not stop.wait(1.0):
        current = watched_snapshot(repo)
        if current == previous:
            continue
        previous = current
        stop.wait(1.0)  # let a save settle before staging it
        try:
            stage = stage_everything(repo)
            collect_data(stage.mirror, repo)
            rewrite(stage)
            _sync_tree(stage.mirror, served)
            print("restaged the docs mirror", flush=True)
        except Exception as error:  # a half-written file is not a reason to stop the preview
            print(f"re-stage failed: {error}", file=sys.stderr)


def preview(repo: Path | None = None) -> int:
    """Stage, then serve a synced copy of the staging tree with live reload.

    Quarto watches the served tree, and a thread watches the sources and
    re-stages on change, so editing `docs/` re-renders the page you are looking
    at without restarting the command.
    """
    repo = Path(repo).resolve() if repo is not None else paths.repo_root()
    stage = stage_everything(repo)
    collect_data(stage.mirror, repo)
    rewrite(stage)
    served = paths.preview_dir(repo)
    _sync_tree(stage.mirror, served)
    stop = threading.Event()
    watcher = threading.Thread(target=_restage_on_change, args=(repo, served, stop), daemon=True)
    watcher.start()
    process = subprocess.Popen(["quarto", "preview", str(served)], env=render_env(stage.commit))
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
