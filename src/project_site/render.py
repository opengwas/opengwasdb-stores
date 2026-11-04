"""Render a Quarto website over several Quarto processes, then merge their output.

Quarto renders a project's pages one after another, and each page costs about a
second whatever its size: its filters and the navigation every page repeats.
The site has over 200 pages, so a render took five minutes on a host with
hundreds of cores. Quarto has no option to render in parallel, so this module
does it from outside:

1. One `quarto render <page>` call renders a single page in a copy of the
   staged project. Quarto runs the project's `pre-render` hook first, so the
   pages the hook writes exist, and `quarto inspect` on that copy lists every
   page the site has.
2. Each worker gets its own copy of that project and renders one page per
   call. A call renders the one page but builds that page's navigation from the
   whole project, so a page is the same whichever worker renders it.
3. The workers' outputs are merged into one site (`merge`).

A worker's output holds more than its own pages, and the merge decides which
copy of each file to believe:

- A page comes from the worker that rendered it. A worker that did not render
  the home page still writes an `index.html`: Quarto's redirect to the first
  page that worker rendered.
- Every other file (the theme, scripts, the resources each call copies) must be
  identical in every worker that holds it. If it is not, the merge stops: there
  is no right copy to choose.
- The search index and the sitemap list only the pages each worker rendered, so
  they are rebuilt page by page, in the order `quarto inspect` lists the pages.
  An entry for a page its worker did not render stops the merge.
- Every page must end up with its HTML; one that has none stops the render.

`quarto render` takes one file or one directory. A `project.render` list in a
profile would name any set of pages, but a sidebar that globs over pages the
list leaves out makes Quarto fail, so each call renders one page. `quarto
inspect` writes Quarto's cache into the project it reads, so only the workers'
copies are inspected: the staged project is never written to.
"""

from __future__ import annotations

import filecmp
import json
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO

# Workers when none is asked for. The render costs about 1,150 CPU-seconds
# whatever the count (226 pages, Nov 2026: 12 workers 91 s, 24 49 s, 32 38 s),
# and the host is shared, so the default takes about a tenth of its 224 cores.
DEFAULT_JOBS = 24

# The site-wide files Quarto writes for the pages a call renders. They are
# rebuilt page by page rather than compared.
SEARCH_INDEX = "search.json"
SITEMAP = "sitemap.xml"

# Quarto's default output directory for a website.
DEFAULT_OUTPUT_DIR = "_site"

# The lines of a failed call's output an error repeats.
FAILURE_TAIL = 40

_SITEMAP_URL = re.compile(r"  <url>\n    <loc>(?P<loc>[^<]*)</loc>\n(?:    <[^\n]*\n)*  </url>\n")
_SITEMAP_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
_SITEMAP_TAIL = "</urlset>\n"


class RenderFailed(RuntimeError):
    """A page failed to render, or the workers' output could not be merged into one site."""


@dataclass
class Part:
    """One worker's output directory and the pages it rendered into it."""

    site: Path
    pages: list[str]


def page_output(page: str) -> str:
    """Where Quarto writes a page: its HTML beside where the source sits."""
    return Path(page).with_suffix(".html").as_posix()


def render_site(
    project: Path,
    output: Path,
    *,
    env: dict[str, str],
    jobs: int = DEFAULT_JOBS,
    out: TextIO = sys.stdout,
) -> int:
    """Render the Quarto website at `project` into `output` over `jobs` workers; return the page count.

    The workers render in copies of `project`, in a directory beside `output`
    that is removed whether the render succeeds or not. `output` must not exist.
    """
    if jobs < 1:
        raise ValueError(f"jobs must be at least 1, not {jobs}")
    project = Path(project).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise RenderFailed(f"{output} already exists; the render writes a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Not a dot-directory: Quarto skips every page under a hidden directory, so
    # inside one it would list no pages at all.
    workspace = Path(tempfile.mkdtemp(prefix=f"{output.name}-workers-", dir=output.parent))
    try:
        first = workspace / "w00"
        _copy_project(project, first, DEFAULT_OUTPUT_DIR)
        before, _config = _inspect(first, env)
        lead = max(before, key=lambda page: (first / page).stat().st_size)
        print(f"rendering {lead} first: Quarto runs the pre-render hook, which may write pages", file=out, flush=True)
        _render_page(first, lead, env)
        pages, config = _inspect(first, env)
        if lead not in pages:
            raise RenderFailed(f"{lead} rendered, but `quarto inspect` no longer lists it as a page")
        output_dir = _output_dir(config)
        site_url = (config.get("website") or {}).get("site-url")
        print(f"[{1:>4}/{len(pages)}] {lead}", file=out, flush=True)

        rest = sorted((page for page in pages if page != lead), key=lambda page: -(first / page).stat().st_size)
        copies = [first]
        for index in range(1, max(1, min(jobs, len(rest)))):
            copies.append(workspace / f"w{index:02d}")
            _copy_project(first, copies[-1], output_dir)
        print(f"rendering {len(pages)} pages over {len(copies)} workers", file=out, flush=True)
        rendered = _render_pages(copies, rest, env, out, done=1, total=len(pages))
        rendered[0].insert(0, lead)
        parts = [Part(site=copy / output_dir, pages=own) for copy, own in zip(copies, rendered)]
        merge(parts, pages, output, site_url=site_url)
        return len(pages)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def merge(parts: Sequence[Part], pages: Sequence[str], output: Path, *, site_url: str | None = None) -> None:
    """Merge the workers' outputs into `output`, following the rules in the module docstring.

    `site_url` is the site's `website.site-url`, which the sitemap's addresses
    begin with; a sitemap cannot be merged without it.
    """
    owner: dict[str, Part] = {}
    for part in parts:
        for page in part.pages:
            if page in owner:
                raise RenderFailed(f"{page} was rendered by two workers")
            owner[page] = part
    unrendered = [page for page in pages if page not in owner]
    if unrendered:
        raise RenderFailed(f"{len(unrendered)} page(s) were never rendered: {', '.join(unrendered[:5])}")
    missing = [page for page in pages if not (owner[page].site / page_output(page)).is_file()]
    if missing:
        raise RenderFailed(
            f"Quarto reported success but wrote no HTML for {len(missing)} page(s): {', '.join(missing[:5])}"
        )
    owned = {page_output(page): owner[page] for page in pages}

    output.mkdir(parents=True, exist_ok=True)
    held: dict[str, Path] = {}
    for part in parts:
        for path in sorted(part.site.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(part.site).as_posix()
            if relative in (SEARCH_INDEX, SITEMAP):
                continue
            if relative in owned:
                if owned[relative] is part:
                    _copy(path, output / relative)
                continue
            if relative in held:
                if not filecmp.cmp(held[relative], path, shallow=False):
                    raise RenderFailed(
                        f"{relative} differs between {held[relative].parent} and {path.parent}: "
                        "every worker should write it the same, so neither copy can be trusted"
                    )
                continue
            held[relative] = path
            _copy(path, output / relative)

    _merge_search(parts, pages, output)
    _merge_sitemap(parts, pages, output, site_url)


def _merge_search(parts: Sequence[Part], pages: Sequence[str], output: Path) -> None:
    entries: dict[str, list[dict[str, Any]]] = {}
    found = False
    for part in parts:
        index = part.site / SEARCH_INDEX
        if not index.is_file():
            continue
        found = True
        own = {page_output(page): page for page in part.pages}
        for entry in json.loads(index.read_text(encoding="utf-8")):
            page_html = str(entry.get("href", "")).split("#", 1)[0]
            if page_html not in own:
                raise RenderFailed(
                    f"{index} has a search entry for {page_html or entry!r}, a page that worker did not render"
                )
            entries.setdefault(own[page_html], []).append(entry)
    if found:
        merged = [entry for page in pages for entry in entries.get(page, [])]
        (output / SEARCH_INDEX).write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")


def _merge_sitemap(
    parts: Sequence[Part], pages: Sequence[str], output: Path, site_url: str | None
) -> None:
    urls: dict[str, str] = {}
    found = False
    for part in parts:
        sitemap = part.site / SITEMAP
        if not sitemap.is_file():
            continue
        found = True
        if not site_url:
            raise RenderFailed(f"{sitemap} exists, but no site-url is known to read its addresses against")
        text = sitemap.read_text(encoding="utf-8")
        matches = list(_SITEMAP_URL.finditer(text))
        if len(matches) != text.count("<url>"):
            raise RenderFailed(f"{sitemap} is not laid out as Quarto writes a sitemap, so it cannot be merged")
        own = {f"{site_url.rstrip('/')}/{page_output(page)}": page for page in part.pages}
        for match in matches:
            if match["loc"] not in own:
                raise RenderFailed(f"{sitemap} lists {match['loc']}, a page that worker did not render")
            urls[own[match["loc"]]] = match[0]
    if found:
        merged = "".join(urls[page] for page in pages if page in urls)
        (output / SITEMAP).write_text(_SITEMAP_HEAD + merged + _SITEMAP_TAIL, encoding="utf-8")


def _render_pages(
    copies: Sequence[Path], pages: Sequence[str], env: dict[str, str], out: TextIO, *, done: int, total: int
) -> list[list[str]]:
    """Render `pages` over the copies, one worker thread per copy; return each copy's pages.

    A failure stops the workers taking new pages; the ones already rendering finish.
    """
    waiting: queue.Queue[str] = queue.Queue()
    for page in pages:
        waiting.put(page)
    rendered: list[list[str]] = [[] for _ in copies]
    failures: list[str] = []
    lock = threading.Lock()
    stop = threading.Event()
    progress = [done]

    def work(index: int) -> None:
        while not stop.is_set():
            try:
                page = waiting.get_nowait()
            except queue.Empty:
                return
            try:
                _render_page(copies[index], page, env)
            except RenderFailed as failure:
                with lock:
                    failures.append(str(failure))
                stop.set()
                return
            with lock:
                rendered[index].append(page)
                progress[0] += 1
                print(f"[{progress[0]:>4}/{total}] {page}", file=out, flush=True)

    threads = [threading.Thread(target=work, args=(index,), daemon=True) for index in range(len(copies))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise RenderFailed("\n\n".join(failures))
    return rendered


def _render_page(copy: Path, page: str, env: dict[str, str]) -> None:
    result = subprocess.run(["quarto", "render", page], cwd=copy, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-FAILURE_TAIL:])
        raise RenderFailed(f"quarto render {page} failed with exit status {result.returncode}:\n{tail}")


def _inspect(project: Path, env: dict[str, str]) -> tuple[list[str], dict[str, Any]]:
    """The project's pages, relative to it, as Quarto lists them, and its resolved configuration."""
    result = subprocess.run(["quarto", "inspect", str(project)], env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RenderFailed(f"quarto inspect {project} failed:\n{result.stderr.strip()}")
    inspected = json.loads(result.stdout)
    listed = inspected["files"]["input"]
    if not listed:
        raise RenderFailed(f"quarto inspect lists no pages in {project}")
    root = project.resolve()
    return [Path(path).resolve().relative_to(root).as_posix() for path in listed], inspected.get("config") or {}


def _output_dir(config: dict[str, Any]) -> str:
    """The project's output directory, which must be a directory directly inside the project."""
    named = (config.get("project") or {}).get("output-dir") or DEFAULT_OUTPUT_DIR
    if Path(named).is_absolute() or len(Path(named).parts) != 1:
        raise RenderFailed(f"the parallel render needs output-dir to be a directory inside the project, not {named!r}")
    return str(named)


def _copy_project(source: Path, destination: Path, output_dir: str) -> None:
    """Copy a project for a worker, without its Quarto cache or output directory (top level only)."""
    root = str(source)

    def skip(directory: str, names: list[str]) -> set[str]:
        return {".quarto", output_dir} & set(names) if directory == root else set()

    shutil.copytree(source, destination, symlinks=True, ignore=skip)


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
