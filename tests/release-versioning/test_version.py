#!/usr/bin/env python3
"""The package version comes from the `v*` tag, not a literal (#386).

Every merge to `main` is tagged `vX.Y.Z` (`.github/workflows/release.yml`), and
`setuptools-scm` turns that tag into the distribution's version at build time.
Two things follow, and each is pinned here:

* **`pyproject.toml` takes the version from the tag.** It has no static
  `version`, it declares `[tool.setuptools_scm]` with the same fallback
  opengwasdb uses, and `setuptools-scm` is a build requirement.
* **The rule produces the versions this repository depends on.** A tag on
  `HEAD` is the version; a commit past the tag, and a repository with no tag at
  all (which is this one's state until the cutover), are development versions;
  a build with no git metadata says `0.0.0+unknown`; and a tag without the `v`
  prefix is not a version, so a stray tag cannot become one.

The mapping is exercised in a throwaway repository under `$TMPDIR` that holds a
copy of this checkout's own `pyproject.toml`, so the config under test is the
real one, and `setuptools_scm.get_version` is the call the build backend makes.
What that deliberately does not cover is pip's isolated build environment; the
last test reads the version this checkout's *installed* distribution reports,
which is the end of the same path.

Run from the repository root:
    python3 tests/release-versioning/test_version.py
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
import warnings
from pathlib import Path

from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The sentinel `[tool.setuptools_scm]` and `CONTRIBUTING.md` both name: valid
#: PEP 440, below every release, and obviously not one.
FALLBACK_VERSION = "0.0.0+unknown"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {result.stderr}")
    return result.stdout.strip()


def pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def scm_config() -> dict:
    """`[tool.setuptools_scm]` as the build backend reads it."""
    return pyproject()["tool"]["setuptools_scm"]


def build_version(root: Path, **overrides: object) -> str:
    """The version a build of `root` would report, from this repo's own config.

    `setuptools_scm.get_version` is what setuptools' build backend calls. It
    does not read `pyproject.toml` itself, so the config is the one this
    checkout declares, passed as it is declared.
    """
    import setuptools_scm

    return setuptools_scm.get_version(root=str(root), **{**scm_config(), **overrides})


class ThrowawayRepositoryTest(unittest.TestCase):
    """A repository that holds nothing but this checkout's `pyproject.toml`."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="release-versioning-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def repository(self, *, git_init: bool = True) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        shutil.copy(REPO_ROOT / "pyproject.toml", repo / "pyproject.toml")
        if git_init:
            git(repo, "init", "-q", "-b", "main")
            self.commit(repo, "base")
        return repo

    def commit(self, repo: Path, message: str) -> str:
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", message)
        return git(repo, "rev-parse", "HEAD")


class PyprojectTest(unittest.TestCase):
    def test_the_version_comes_from_the_tag_and_not_from_a_literal(self) -> None:
        project = pyproject()["project"]
        self.assertEqual(project["dynamic"], ["version"], "the version must be dynamic, or a literal shadows the tag")
        self.assertNotIn("version", project)

    def test_the_build_requires_setuptools_scm(self) -> None:
        requires = pyproject()["build-system"]["requires"]
        self.assertTrue(
            any(requirement.split(">")[0].split("=")[0].strip() == "setuptools-scm" for requirement in requires),
            f"a dynamic version needs setuptools-scm in build-system.requires: {requires}",
        )

    def test_the_fallback_cannot_be_mistaken_for_a_release(self) -> None:
        """`0.0.0+unknown` is the sentinel opengwasdb uses too (#333, #386)."""
        config = scm_config()
        self.assertEqual(config["fallback_version"], FALLBACK_VERSION)
        fallback = Version(config["fallback_version"])
        self.assertEqual(fallback.public, "0.0.0")
        self.assertEqual(fallback.local, "unknown")
        self.assertLess(fallback, Version("0.0.1"))

    def test_one_tag_convention_is_declared(self) -> None:
        """Only `vX.Y.Z` is a version, so a stray tag cannot become one."""
        self.assertEqual(scm_config()["tag_regex"], r"^v(?P<version>.*)$")


class VersionFromTagTest(ThrowawayRepositoryTest):
    def test_a_tag_on_head_is_the_version(self) -> None:
        repo = self.repository()
        git(repo, "tag", "-a", "v9.9.9", "-m", "release")
        self.assertEqual(build_version(repo), "9.9.9")

    def test_a_commit_past_the_tag_is_a_development_version(self) -> None:
        repo = self.repository()
        git(repo, "tag", "-a", "v9.9.9", "-m", "release")
        self.commit(repo, "one past the tag")

        reported = Version(build_version(repo))
        self.assertTrue(reported.is_devrelease, reported)
        self.assertGreater(reported, Version("9.9.9"))
        self.assertEqual(reported.release, (9, 9, 10), "the bump setuptools-scm guesses past a release")
        self.assertTrue((reported.local or "").startswith("g"), "a clean checkout names its commit")

    def test_a_repository_without_a_tag_is_a_development_version(self) -> None:
        """This repository's state until the cutover: no `v*` tag exists yet.

        `0.1.devN+g<hash>` also says, correctly, that nothing has been released:
        it is below every `0.1.0` a first tag would make.
        """
        reported = Version(build_version(self.repository()))
        self.assertTrue(reported.is_devrelease, reported)
        self.assertEqual(reported.release, (0, 1))
        self.assertTrue(reported.public.startswith("0.1.dev1"), reported)

    def test_a_build_without_git_metadata_is_the_fallback(self) -> None:
        """An sdist built outside a checkout still has to produce something."""
        self.assertEqual(build_version(self.repository(git_init=False)), FALLBACK_VERSION)

    def test_a_tag_without_the_v_prefix_is_not_a_version(self) -> None:
        repo = self.repository()
        git(repo, "tag", "-a", "9.9.9", "-m", "a tag nobody should push")

        try:
            with warnings.catch_warnings():
                # `setuptools-scm` warns about the tag it cannot read; the
                # assertion below is this test's own statement of that.
                warnings.simplefilter("ignore")
                reported = Version(build_version(repo))
        except (LookupError, AssertionError, ValueError):
            # `setuptools-scm` refuses a tag it cannot parse. A refusal is the
            # point; which exception it raises is its business.
            return
        self.assertTrue(reported.is_devrelease, f"{reported} came from a tag that is not a version")


class InstalledDistributionTest(unittest.TestCase):
    def test_the_installed_distribution_reports_a_version_from_the_tag(self) -> None:
        """The end of the same path: what `importlib.metadata` says here.

        The environment was installed from this checkout, so its version is the
        one `setuptools-scm` computed then. A tag on `HEAD` must be that
        version; past the tag it must be a development version above the tag.
        A stale editable environment reports an older development version, so
        the message says to reinstall rather than blaming the rule.
        """
        reported = Version(importlib.metadata.version("ogstores"))
        tags = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
            cwd=REPO_ROOT, env=GIT_ENV, capture_output=True, text=True,
        )
        if tags.returncode != 0:
            # No `v*` tag exists yet: this repository's state until the cutover.
            self.assertTrue(reported.is_devrelease, f"no tag exists, so {reported} cannot be a release")
            self.assertEqual(reported.release, (0, 1), reported)
            return

        tag = tags.stdout.strip()
        tagged = Version(tag.removeprefix("v"))
        head = git(REPO_ROOT, "rev-parse", "HEAD")
        if head == git(REPO_ROOT, "rev-parse", f"{tag}^{{commit}}"):
            self.assertEqual(reported, tagged, f"HEAD is {tag} but the installed metadata says {reported}; run `pixi install`")
        else:
            self.assertGreater(reported, tagged, f"the last tag is {tag} but the installed metadata says {reported}; run `pixi install`")
            self.assertTrue(reported.is_devrelease, reported)


if __name__ == "__main__":
    unittest.main()
