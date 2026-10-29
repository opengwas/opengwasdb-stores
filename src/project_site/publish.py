"""Publish a site render of a ref to the `gh-pages` branch (#388).

The site is published from `gh-pages`, served at
blog.opengwas.io/opengwasdb-stores/, and rendered on the host where its data
lives. A publish therefore renders a clean detached checkout of the ref itself,
never a developer's working tree, and commits the output to `gh-pages` with a
message naming the commit it came from.

`--dry-run` does everything except the push: it renders, commits `gh-pages`
locally, and leaves the branch and the worktree to inspect. Publishing without
`--dry-run` is the owner's, so nothing here runs it by accident.

    pixi run site-publish --dry-run
    pixi run site-publish --ref origin/dev --dry-run

The ref must exist on the remote, and an existing `gh-pages` worktree must be
clean: a publish overwrites what it holds, so uncommitted edits there are
refused rather than lost.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from project_site import paths

Render = Callable[[str, Path, Path], Path]


class PublishRefused(Exception):
    """The publish asked for something it must not do: an unremote ref, a dirty tree."""


@dataclass(frozen=True)
class PublishResult:
    """What a publish did: the ref and commit it rendered, and where it put it."""

    ref: str
    source_commit: str
    gh_pages_commit: str | None
    message: str
    dry_run: bool
    pushed: bool


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    ).stdout.strip()


def _remote_commit(repo: Path, remote: str, ref: str) -> str:
    """The commit `ref` names, refusing a ref the remote does not have."""
    if not ref.startswith(f"{remote}/"):
        raise PublishRefused(f"--ref {ref} is not a {remote}/... ref")
    branch = ref[len(remote) + 1:]
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not listed:
        raise PublishRefused(f"{ref} is not on {remote}")
    return _git(repo, "rev-parse", f"{ref}^{{commit}}")


def _worktree_paths(repo: Path) -> set[Path]:
    listed = _git(repo, "worktree", "list", "--porcelain").splitlines()
    return {Path(line.split(" ", 1)[1]).resolve() for line in listed if line.startswith("worktree ")}


def _prepare_gh_pages_worktree(repo: Path, checkout: Path, source_commit: str) -> Path:
    """The `gh-pages` worktree, reused when it exists and created as an orphan when not."""
    if checkout.exists() or checkout.is_symlink():
        if checkout.resolve() not in _worktree_paths(repo):
            raise PublishRefused(f"{checkout} exists and is not a git worktree")
        if _git(checkout, "rev-parse", "--abbrev-ref", "HEAD") != "gh-pages":
            raise PublishRefused(f"{checkout} is not on gh-pages")
        dirty = _git(checkout, "status", "--porcelain")
        if dirty:
            raise PublishRefused(f"the gh-pages worktree at {checkout} has uncommitted changes:\n{dirty}")
        return checkout
    has_branch = (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "refs/heads/gh-pages"],
            capture_output=True,
        ).returncode
        == 0
    )
    if has_branch:
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--quiet", str(checkout), "gh-pages"], check=True)
    else:
        # No gh-pages yet: check the source commit out detached, then make an
        # orphan branch from it, so the published branch shares no history.
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--quiet", "--detach", str(checkout), source_commit],
            check=True,
        )
        subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", "--orphan", "gh-pages"], check=True)
    return checkout


def _replace_contents(checkout: Path, rendered: Path) -> None:
    """Replace everything the worktree holds with the render, `.git` aside."""
    for entry in checkout.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in rendered.iterdir():
        destination = checkout / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination)
        else:
            shutil.copy2(entry, destination)


def publish(
    *,
    repo: Path,
    ref: str,
    remote: str = "origin",
    dry_run: bool,
    render: Render | None = None,
    work_root: Path | None = None,
) -> PublishResult:
    """Render `ref` in a clean checkout and commit it to `gh-pages`, pushing unless `dry_run`."""
    repo = Path(repo).resolve()
    work_root = Path(work_root).resolve() if work_root is not None else paths.work_root()
    name = paths.checkout_name(repo)
    work_root.mkdir(parents=True, exist_ok=True)

    try:
        _git(repo, "fetch", "--quiet", remote)
    except subprocess.CalledProcessError as error:
        raise PublishRefused(f"cannot fetch {remote}: {error.stderr.strip()}") from error
    source_commit = _remote_commit(repo, remote, ref)

    if render is None:
        from project_site.build import render_checkout

        render = render_checkout

    checkout = work_root / f"{name}-publish"
    if checkout.exists():
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(checkout)], check=False)
        if checkout.exists():
            shutil.rmtree(checkout)
    output = work_root / f"{name}-publish-site"
    if output.exists():
        shutil.rmtree(output)

    gh_pages = work_root / f"{name}-gh-pages"
    try:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--quiet", "--detach", str(checkout), source_commit],
            check=True,
        )
        rendered = Path(render(source_commit, checkout, output))
        if not rendered.is_dir():
            raise PublishRefused(f"the render wrote no directory at {rendered}")
        _prepare_gh_pages_worktree(repo, gh_pages, source_commit)
        _replace_contents(gh_pages, rendered)
        _git(gh_pages, "add", "-A", "--", ".")
        message = f"Publish the site from {ref} at {source_commit}"
        _git(gh_pages, "commit", "--quiet", "--message", message, "--allow-empty")
        gh_pages_commit = _git(gh_pages, "rev-parse", "HEAD")
        pushed = False
        if not dry_run:
            _git(gh_pages, "push", "--quiet", remote, "HEAD:refs/heads/gh-pages")
            pushed = True
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(checkout)], check=False)
        if output.exists():
            shutil.rmtree(output)

    return PublishResult(
        ref=ref,
        source_commit=source_commit,
        gh_pages_commit=gh_pages_commit,
        message=message,
        dry_run=dry_run,
        pushed=pushed,
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default=paths.DEFAULT_PUBLISH_REF, help="remote-tracking ref to publish")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--repo", type=Path, default=paths.repo_root())
    parser.add_argument("--dry-run", action="store_true", help="render and commit gh-pages, but do not push")
    args = parser.parse_args(argv)

    try:
        result = publish(repo=args.repo, ref=args.ref, remote=args.remote, dry_run=args.dry_run)
    except PublishRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    print(f"ref          {result.ref}")
    print(f"source       {result.source_commit}")
    print(f"gh-pages     {result.gh_pages_commit}")
    print(f"message      {result.message}")
    print("push" + ("                     (dry run: nothing pushed)" if result.dry_run else f"  -> {args.remote} gh-pages"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
