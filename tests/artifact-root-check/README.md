# The Artifact Root check on a fixture registry

Tests for #510's `ogstores.artifact_root_check`, which compares the committed
Release Bundles with the Artifact Root, read-only. Each fixture is a temporary
registry of minimal Release Bundles and a temporary Artifact Root holding small
stand-in Stores, `by-label` links, markers and locks, so nothing is built.

1. One case of each category gives exactly those findings: a `built` bundle
   with no Store, a Store directory with no bundle, a Store whose bundle is
   `accepted`, a `by-label` link whose target is gone, and a pending
   publication beside a staged Store. Withdrawn and superseded bundles with a
   Store disagree; an `accepted` bundle mid-build and non-release directories
   (`catalog/`, `.tmp/`) are not findings; a hidden staged file and a live run's
   lock are described as the release workflow describes them.
2. The exit status is 1 for category 1 or 4, and 0 otherwise. It is 2, with
   the cause named and no report written, for an Artifact Root that is not a
   directory, a missing or empty registry, an `OSError` reading the root, a
   Release Status the check's split does not cover (a monkeypatched
   `VALID_STATUSES`), and a `--json` or `--markdown` path under the root,
   directly or through a link.
3. A run changes no byte, mtime, mode or link target under the fixture root.
4. The report records the time, host, Artifact Root, the registry's git
   revision and dirty state, and each finding with its category and Store id,
   and no path outside `<root>/<id>`; a label pointing outside the root is
   reported without its target.
5. A root given as `./x/` or through a link, and a link holding an absolute
   target through another spelling of the root, still name the target's Store.

    pixi run -e dev python3 tests/artifact-root-check/test_artifact_root_check.py
