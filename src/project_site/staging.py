"""Stage what the site renders: the engine's docs, the glossary, and a docs mirror (#388).

The site is one Quarto project over two repositories' documentation, so a render
needs a tree it may alter. This module builds that tree:

- the engine's `docs/` at the revision `pixi.lock` pins, from a bare clone under
  the work root, keeping only its Markdown and self-contained HTML;
- `CONTEXT.md` as the glossary page;
- a mirror of this repository's `docs/` without `reports/`, whose `.qmd` sources
  are report sources the site does not execute;
- over the mirror, the link rewrite and the Mermaid fence spelling from
  `project_site.links`, so no source file is ever edited.

`git archive` is what stages a revision, so the tree is exactly that commit's,
not whatever a working checkout happens to hold.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from project_site.links import convert_mermaid_fences, rewrite_document

# Documentation, not data: the engine's `docs/` also holds logs, JSON, JSONL and
# the `.qmd` sources whose reports are published as HTML beside them.
DOCUMENT_SUFFIXES = frozenset({".md", ".html"})

# Directories under `docs/` the mirror leaves out: report sources (the site
# copies their committed HTML instead), Quarto's own cache, and anything this
# build writes.
MIRROR_SKIP_DIRECTORIES = frozenset({"reports", ".quarto", "_site"})

# Files at the top of the engine's docs tree that are not a page of the engine
# section's index.
ENGINE_INDEX = "index.qmd"


@dataclass
class LinkReport:
    """What the link rewrite did to the mirror, for the build's summary.

    `raw_pages` are pages the render could not read as Markdown: Quarto only
    accepts an executable cell (`{mermaid}`) in a `.qmd`, so a `.md` page with a
    Mermaid fence is staged as `.qmd` and its rewritten Markdown is kept here, to
    be copied to its own published `.md` path after the render.
    """

    pages: int = 0
    rewritten: int = 0
    missing: list[tuple[str, str, str]] = field(default_factory=list)
    raw_pages: list[tuple[str, str]] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _documentation_files(repo: Path, revision: str) -> list[str]:
    names = _git(repo, "ls-tree", "-r", "--name-only", revision, "--", "docs").splitlines()
    return [
        name
        for name in names
        if name.startswith("docs/") and Path(name).suffix.lower() in DOCUMENT_SUFFIXES
    ]


def stage_docs(repo: Path, revision: str, dest: Path) -> list[str]:
    """Extract `repo`'s `docs/` at `revision` into `dest`, keeping documentation only.

    Returns the staged paths relative to `dest`, sorted. `dest` is rebuilt, so a
    change of pinned revision never leaves a page behind that the revision no
    longer has.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    files = _documentation_files(repo, revision)
    if not files:
        return []
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", revision, "--", *files],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = []
        for member in tar.getmembers():
            if not member.isfile():
                continue
            member.name = member.name[len("docs/"):]
            members.append(member)
        tar.extractall(dest, members=members, filter="data")
    return sorted(path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file())


def ensure_engine_clone(clone: Path, revision: str, *, url: str) -> Path:
    """A bare clone of the engine holding `revision`, cloned or fetched as needed."""
    if not (clone / "HEAD").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--bare", "--quiet", url, str(clone)], check=True)
    if not _has_revision(clone, revision):
        subprocess.run(["git", "-C", str(clone), "fetch", "--quiet", "origin"], check=True)
    if not _has_revision(clone, revision):
        raise SystemExit(f"the engine clone at {clone} does not have revision {revision}")
    return clone


def _has_revision(clone: Path, revision: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(clone), "cat-file", "-e", f"{revision}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


def stage_glossary(context: Path, dest: Path) -> Path:
    """Copy `CONTEXT.md` to the page the site publishes as its glossary."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(context, dest)
    return dest


def mirror_docs(repo: Path, dest: Path) -> list[str]:
    """Copy `repo`'s `docs/` to `dest`, leaving out the directories the site builds."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = set()
        for name in names:
            if name in MIRROR_SKIP_DIRECTORIES and Path(directory, name).is_dir():
                skipped.add(name)
        return skipped

    shutil.copytree(repo / "docs", dest, ignore=ignore)
    return sorted(path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file())


def rewrite_mirror(
    dest: Path,
    *,
    registry: Path,
    commit: str,
    registry_url: str,
    engine: Path,
    engine_commit: str,
    engine_url: str,
    engine_prefix: str,
) -> LinkReport:
    """Rewrite the mirror's outbound links, spelling Mermaid fences for Quarto.

    A page under `engine_prefix` came from the engine at `engine_commit`; every
    other page came from this repository at `commit`. A rewritten target that the
    commit does not have is reported rather than hidden: its GitHub link will 404
    exactly as the relative one would have.
    """
    registry_paths = set(_git(registry, "ls-tree", "-r", "--name-only", commit).splitlines())
    engine_paths = set(_git(engine, "ls-tree", "-r", "--name-only", engine_commit).splitlines())

    report = LinkReport()
    for path in sorted(dest.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".qmd"}:
            continue
        page = path.relative_to(dest).as_posix()
        if page.startswith(engine_prefix):
            base, known = (engine_url, engine_commit), engine_paths
            relative = page[len(engine_prefix):]
        else:
            base, known = (registry_url, commit), registry_paths
            relative = page
        text = path.read_text(encoding="utf-8")
        converted = convert_mermaid_fences(text)
        text, targets = rewrite_document(
            converted, page=relative, repo_url=base[0], commit=base[1]
        )
        if path.suffix.lower() == ".md" and _has_mermaid_cell(converted):
            # Quarto refuses a `{mermaid}` cell in a `.md`, so the staged page is
            # a `.qmd`; its published `.md` path is filled from `raw_pages`.
            path.with_suffix(".qmd").write_text(text, encoding="utf-8")
            path.unlink()
            report.raw_pages.append((page, text))
        else:
            path.write_text(text, encoding="utf-8")
        report.pages += 1
        for target in targets:
            report.rewritten += 1
            if target.repo_path and known and target.repo_path not in known:
                report.missing.append((page, target.target, target.repo_path))
    return report


def _has_mermaid_cell(text: str) -> bool:
    """Whether the text holds a `{mermaid}` cell after the fence conversion."""
    return any(line.strip() == "```{mermaid}" for line in text.splitlines())


