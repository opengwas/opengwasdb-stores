- **The site now draws the architecture of both repositories: a Pipeline page and
  a Structure and health page (issue #418, epic #416).** Each repository keeps a
  hand-written `docs/architecture/components.yaml` whose components claim code
  paths; this repository's test fails on a code file nobody claims or a claim
  that matches nothing. At site build a collector reads this repository at
  `HEAD` and the engine at its `main`, reports the engine's gaps without failing
  the build, counts the imports between components across both repositories,
  marks those that go against the engine's layering, and places each ADR by the
  code it cites and by component names in its title. The pages draw it with
  Quarto `{dot}` cells, so nothing generated is committed, and each states what
  is written by hand and what is measured.
