#!/usr/bin/env python3
"""The package version comes from the `v*` tag, not a literal (#386).

Every merge to `main` is tagged `vX.Y.Z` (`.github/workflows/release.yml`), and
`setuptools-scm` turns that tag into the distribution's version at build time.
Three things follow, and each is pinned here:

* **`pyproject.toml` takes the version from the tag.** It has no static
  `version`, it declares `[tool.setuptools_scm]` with the same fallback
  opengwasdb uses, and `setuptools-scm` is a build requirement.
* **The tag rule produces the versions this repository depends on.** A tag on
  `HEAD` is the version; a commit past the tag, and a repository with no tag at
  all (which is this one's state until the cutover), are development versions;
  a build with no git metadata says `0.0.0+unknown`; and a tag without the `v`
  prefix is not a version, so a stray tag cannot become one.
* **`git describe` is restricted to `v*`.** `tag_regex` decides what can be
  *parsed*, not what describe *selects*, so the glob is configured as well and
  the restriction is shown to matter against an unfiltered command.

The mapping is exercised in a throwaway repository under `$TMPDIR` that holds a
copy of this checkout's own `pyproject.toml`, so the config under test is the
real one, and `setuptools_scm.get_version` is the call the build backend makes.

What none of it can check is *this environment's* answer: an editable install
records the version at the commit it was installed from, so a cached or stale
one answers for an older tree. `.github/workflows/ci.yml` checks that step
instead, right after pixi has built the package from the tagged checkout.

Run from the repository root:
    python3 tests/release-versioning/test_version.py
"""

from __future__ import annotations

import os
import re
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


def describe_command() -> list[str]:
    return list(scm_config()["git_describe_command"])


def build_version(root: Path, **overrides: object) -> str:
    """The version a build of `root` would report, from this repo's own config.

    `setuptools_scm.get_version` is what setuptools' build backend calls. It
    does not read `pyproject.toml` itself, so the config is the one this
    checkout declares, passed as it is declared.
    """
    import setuptools_scm

    return setuptools_scm.get_version(root=str(root), **{**scm_config(), **overrides})


class TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="release-versioning-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def repository(self, *, git_init: bool = True, tag: str | None = None,
                   past: bool = False, stray: str | None = None) -> Path:
        """A repository holding nothing but this checkout's `pyproject.toml`."""
        repo = self.root / "repo"
        repo.mkdir()
        shutil.copy(REPO_ROOT / "pyproject.toml", repo / "pyproject.toml")
        if not git_init:
            return repo
        git(repo, "init", "-q", "-b", "main")
        self.commit(repo, "base")
        if tag is not None:
            git(repo, "tag", "-a", tag, "-m", f"Release {tag}")
        if past:
            self.commit(repo, "one past the tag")
        if stray is not None:
            git(repo, "tag", "-a", stray, "-m", "a tag nobody should push")
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

    def test_git_describe_is_restricted_to_release_tags(self) -> None:
        """Parsing `v*` is not enough: `git describe` must not *select* another.

        A stray `99.0.0` tag on `HEAD` made the build die with "Can't parse
        version from tag '99.0.0'" instead of using the newest `v*` tag:
        `tag_regex` restricts parsing only, and describe's default glob accepts
        it. The configured command is what decides, so it is run below.
        """
        command = describe_command()
        self.assertIn("--match", command)
        self.assertEqual(command[command.index("--match") + 1], "v*")


class VersionFromTagTest(TemporaryDirectoryTest):
    def test_a_tag_on_head_is_the_version(self) -> None:
        self.assertEqual(build_version(self.repository(tag="v9.9.9")), "9.9.9")

    def test_a_commit_past_the_tag_is_a_development_version(self) -> None:
        reported = Version(build_version(self.repository(tag="v9.9.9", past=True)))
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
        reported = self.version_of(self.repository(stray="9.9.9"))
        self.assertTrue(reported.is_devrelease, f"{reported} came from a tag that is not a version")

    def test_the_configured_describe_ignores_a_stray_tag(self) -> None:
        """A commit carrying both `v0.2.0` and a stray `99.0.0` describes as the release."""
        repo = self.repository(tag="v0.2.0", past=True, stray="99.0.0")

        described = subprocess.run(
            describe_command(), cwd=repo, env=GIT_ENV, capture_output=True, text=True, check=True
        ).stdout.strip()

        self.assertTrue(described.startswith("v0.2.0-1-g"), described)
        self.assertNotIn("99.0.0", described)

    def test_without_its_match_the_command_would_take_the_stray_tag(self) -> None:
        """The negative that makes the test above mean something."""
        repo = self.repository(tag="v0.2.0", past=True, stray="99.0.0")
        unfiltered = [argument for argument in describe_command() if argument not in {"--match", "v*"}]

        described = subprocess.run(
            unfiltered, cwd=repo, env=GIT_ENV, capture_output=True, text=True, check=True
        ).stdout.strip()

        self.assertTrue(described.startswith("99.0.0-0-g"), described)

    def test_the_stray_tag_does_not_reach_the_version(self) -> None:
        """With both tags present, the build reports the release lineage, not the stray."""
        reported = Version(build_version(self.repository(tag="v0.2.0", past=True, stray="99.0.0")))
        self.assertTrue(reported.is_devrelease, reported)
        self.assertEqual(reported.release, (0, 2, 1))

    def test_the_tag_on_head_parses_to_its_version(self) -> None:
        """A tag on `HEAD` is the version, through the configured regex."""
        repo = self.repository(tag="v0.2.0")
        exact = git(repo, "describe", "--tags", "--exact-match", "v0.2.0")

        match = re.compile(str(scm_config()["tag_regex"])).match(exact)

        self.assertIsNotNone(match)
        self.assertEqual(match["version"], "0.2.0")

    @staticmethod
    def version_of(repo: Path) -> Version:
        try:
            with warnings.catch_warnings():
                # `setuptools-scm` warns about a tag it cannot read; the
                # assertion is this test's own statement of that.
                warnings.simplefilter("ignore")
                return Version(build_version(repo))
        except (LookupError, AssertionError, ValueError):
            # A refusal is the point; which exception it raises is its business.
            return Version("0.0.0+refused")


if __name__ == "__main__":
    unittest.main()
