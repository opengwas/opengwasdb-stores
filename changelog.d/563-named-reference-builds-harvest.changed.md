- **A build given a `--variant-reference` that names some rsids now harvests the
  rest, so it can refuse and costs about 30 % more (opengwasdb#258, issue #563).**
  Named-reference `build-hybrid` and `build-dense-vcf` runs read their sources in
  Pass 1 as every other build does: an hg19 source is lifted under
  `--liftover-failure-threshold` and a build past it refuses, and under
  `--on-rsid-conflict refuse` a dispute among the sources refuses even at a
  variant the reference names. Measured on OGS-00004, the harvest is 13.5 of a 44
  min build, and on OGS-00005 7 of 31 min; an artifact `extract-variant-reference`
  made pays Pass 1 twice. `observed.rsid_conflicts` is `null` on a record written
  before #258 only. `docs/runtime.md`, `docs/release-metadata-schema.md` and
  `docs/spec/store-release-workflow.md` say so.
