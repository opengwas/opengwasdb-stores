- **The Artifact Root check compares the Store Registry with the disk (issue
  #510, epic og-dev-hub#14).** `pixi run check-artifact-root` reads the
  committed Release Bundles and the Artifact Root, read-only, and reports a
  `built` or `validated` bundle with no Store, a release directory with no
  bundle, a Store whose bundle is not built or is retired, a broken `by-label`
  link, and a pending publication or staged Store. It exits 1 for a missing
  Store or a broken link, 0 otherwise, and 2 when it refuses (a missing or
  empty registry, an unreadable root). `--json`/`--markdown` write a report
  that records the time, host, Artifact Root and registry revision, and no
  path outside `<root>/<id>`. Its first run is committed as `docs/artifact-root-check.md`:
  OGS-00022 and OGS-00023 are registered `validated` with no Store. Run it
  before and after any change to Artifact Root paths and at a Store epic's
  finish (`workflow/README.md`; `docs/spec/store-release-workflow.md`, "The
  Artifact Root check").
