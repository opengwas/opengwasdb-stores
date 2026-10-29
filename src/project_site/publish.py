"""Publish a site render of a ref to the `gh-pages` branch (#388).

The site is published from `gh-pages`, served at
blog.opengwas.io/opengwasdb-stores/, and rendered on the host where its data
lives. A publish therefore renders a clean detached checkout of the ref itself,
never a developer's working tree, and commits the output to `gh-pages` with a
message naming the commit it came from.

`--dry-run` does everything except the push: it renders, commits `gh-pages`
locally, and leaves the branch to inspect. Publishing without `--dry-run` is the
owner's, so nothing here runs it by accident.

    pixi run site-publish --dry-run
    pixi run site-publish --ref origin/dev --dry-run

Every directory a publish writes to is created by that run, under the work root,
with a name no other run will choose; a publish never writes to a path that
already exists. Cleanup removes only those, and a worktree it created goes
through `git worktree remove` so git keeps the last word: if git refuses, the
path is reported and left alone rather than deleted.

The ref must exist on the remote, and the `gh-pages` branch must not be checked
out in a worktree this run did not create; a publish replaces the contents of
its own `gh-pages` worktree, so someone else's edits there would be lost.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from project_site import paths
from project_site.build import SiteCheckFailed, render_checkout

Render = Callable[[str, Path, Path], Path]


class PublishRefused(Exception):
    """The publish asked for something it must not do: an unremote ref, a held branch."""


@dataclass(frozen=True)
class PublishResult:
    """What a publish did: the ref and commit it rendered, and where it put it."""

    ref: str
    source_commit: str
    gh_pages_commit: str | None
    message: str
    dry_run: bool
    pushed: bool
    scratch: tuple[Path, ...] = ()
    leftovers: tuple[str, ...] = ()


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


def _worktrees(repo: Path) -> list[tuple[Path, str]]:
    """Every worktree of `repo` and the branch it has checked out ('' when detached)."""
    found: list[tuple[Path, str]] = []
    path: Path | None = None
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
            found.append((path, ""))
        elif line.startswith("branch ") and path is not None:
            found[-1] = (path, line[len("branch "):])
    return found


def _refuse_a_held_gh_pages(repo: Path) -> None:
    """Refuse when `gh-pages` is checked out somewhere this run did not create it."""
    for path, branch in _worktrees(repo):
        if branch != "refs/heads/gh-pages":
            continue
        dirty = _git(path, "status", "--porcelain")
        state = "has uncommitted changes" if dirty else "is checked out"
        raise PublishRefused(
            f"the gh-pages branch {state} at {path}; a publish replaces the contents of its own "
            "gh-pages worktree, so remove that worktree first"
        )


def _scratch(work_root: Path, name: str, kind: str) -> Path:
    """A directory this run owns: unique, and empty so `git worktree add` accepts it."""
    work_root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"{name}-{kind}-", dir=work_root))
    path.rmdir()
    if path.exists():  # pragma: no cover - mkdtemp names are unique
        raise PublishRefused(f"{path} already exists; a publish only writes where it created a path")
    return path


def _remove_worktree(repo: Path, worktree: Path) -> str | None:
    """Remove a worktree this run created; git refuses to lose work, and so do we."""
    if not worktree.exists():
        return None
    completed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", str(worktree)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return (
            f"could not remove the scratch worktree at {worktree}, so it was left in place: "
            f"{completed.stderr.strip()}"
        )
    return None


def _fill_worktree(worktree: Path, rendered: Path) -> None:
    """Replace what the worktree holds with the render. Only for a worktree this run made."""
    for entry in worktree.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in rendered.iterdir():
        destination = worktree / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination)
        else:
            shutil.copy2(entry, destination)


def _has_gh_pages_branch(repo: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "refs/heads/gh-pages"],
            capture_output=True,
        ).returncode
        == 0
    )


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

    try:
        _git(repo, "fetch", "--quiet", remote)
    except subprocess.CalledProcessError as error:
        raise PublishRefused(f"cannot fetch {remote}: {error.stderr.strip()}") from error
    source_commit = _remote_commit(repo, remote, ref)
    _refuse_a_held_gh_pages(repo)
    render = render or render_checkout

    checkout = _scratch(work_root, name, "publish")
    output = _scratch(work_root, name, "site")
    published = _scratch(work_root, name, "gh-pages")
    # The render reads a mirror and writes its output; both are this run's.
    derived = [output, output.with_name(f"{output.name}-stage")]
    made: list[Path] = [checkout, published]
    leftovers: list[str] = []
    try:
        _git(repo, "worktree", "add", "--quiet", "--detach", str(checkout), source_commit)
        rendered = Path(render(source_commit, checkout, output))
        if not rendered.is_dir():
            raise PublishRefused(f"the render wrote no directory at {rendered}")
        if _has_gh_pages_branch(repo):
            _git(repo, "worktree", "add", "--quiet", str(published), "gh-pages")
        else:
            # No gh-pages yet: create it as an orphan, so the published branch
            # shares no history with the source branch.
            _git(repo, "worktree", "add", "--quiet", "--orphan", "-b", "gh-pages", str(published))
        _fill_worktree(published, rendered)
        _git(published, "add", "-A", "--", ".")
        message = f"Publish the site from {ref} at {source_commit}"
        _git(published, "commit", "--quiet", "--message", message, "--allow-empty")
        gh_pages_commit = _git(published, "rev-parse", "HEAD")
        pushed = False
        if not dry_run:
            _git(published, "push", "--quiet", remote, "HEAD:refs/heads/gh-pages")
            pushed = True
    finally:
        for worktree in made:
            problem = _remove_worktree(repo, worktree)
            if problem:
                leftovers.append(problem)
        for path in derived:
            if path.exists():
                shutil.rmtree(path)
        for problem in leftovers:
            print(problem, file=sys.stderr)

    return PublishResult(
        ref=ref,
        source_commit=source_commit,
        gh_pages_commit=gh_pages_commit,
        message=message,
        dry_run=dry_run,
        pushed=pushed,
        scratch=(checkout, output, published),
        leftovers=tuple(leftovers),
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
    except (PublishRefused, SiteCheckFailed) as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    print(f"ref          {result.ref}")
    print(f"source       {result.source_commit}")
    print(f"gh-pages     {result.gh_pages_commit}")
    print(f"message      {result.message}")
    print("push" + ("                     (dry run: nothing pushed)" if result.dry_run else f"  -> {args.remote} gh-pages"))
    if result.leftovers:
        print("scratch      left in place; see the messages above")
        return 1
    return 0
