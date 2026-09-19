# Planning is pure; artifact existence is a runtime fact

Amends [0023](0023-the-registry-store-seam-is-a-command-line.md) with one bounded,
syntactic exception to its "options are rendered verbatim, never interpreted" rule.

A Build Recipe may declare an optional `variant_reference` pre-build stage for the
Dense-VCF and Hybrid builders (#145): the release wants a variant axis extracted
from its own derived build manifest before the store build runs:

```yaml
build:
  command: build-dense-vcf
  options:
    variant-reference: /artifact/OGS-00042/work/variant-ref.tsv.gz
  variant_reference:
    output: /artifact/OGS-00042/work/variant-ref.tsv.gz
    options:
      n-workers: 16
```

The stage has one property the rest of the seam does not: the artifact it names
may already exist. It can be a shared panel computed once for many releases, a
plain ALID list, or a store Variant Table -- `opengwasdb` distinguishes these by
content, not suffix. Whether the file is present is a fact about the build host at
the moment the step runs, not a fact about the accepted bundle, which is fixed
input. That mismatch is the whole decision: where does the existence check go?

## Decision

- **`plan()` stays pure.** It emits the `extract-variant-reference` Step whenever
  the Build Recipe declares `variant_reference`, and does nothing else. It resolves
  the derived build manifest path and the declared destination with `paths.py`,
  appends the destination to the build step's inputs, and performs no filesystem
  I/O. It never asks whether the destination exists. The same bundle yields the
  same `[Step]` on every host, every run.
- **The executor decides at runtime.** `run.execute_step` checks the declared
  destination when the step actually runs. If it is present, no subprocess is
  launched and a success record is written with `skipped: true` and
  `skip_reason: provided`. If it is absent, `opengwasdb extract-variant-reference`
  runs under the existing process-group supervision and timeout, writing to a
  staged sibling that is atomically renamed into place on success and removed on
  failure or interruption. A partial artifact therefore never satisfies a later
  existence check.
- **`register` records the provenance.** The assembled `validation.yaml` states
  whether the variant reference was `provided` (the step skipped, or a build
  option named a panel with no declared pre-stage, as OGS-00004/OGS-00005 do) or
  `extracted` (the step ran). A release that uses no variant reference carries no
  such key.
- **One bounded cross-check, the syntactic exception to ADR 0023.** `plan()`
  compares the declared `variant_reference` destination with the build step's
  `variant-reference` option and rejects a Build Recipe whose two copies of the
  same path disagree, or that declares the pre-stage without the option. ADR 0023
  says an option key is rendered, never interpreted; this is the single sanctioned
  exception, because two paths for one artifact must not be allowed to diverge
  silently. The check is syntactic -- path equality on the declared strings, no
  filesystem access -- so it preserves the purity the decision rests on.

## Why planning stays pure

- **Testability by string comparison.** The plan suite asserts `plan()`'s output
  against golden argv files. A deterministic function with no host input is exactly
  what a golden comparison can pin; a planner that stat'ed the filesystem would
  make the same bundle plan differently on CI and on the production host.
- **Reproducibility.** The accepted bundle is immutable and is the only input.
  An existence check at planning time would fold host state into the plan, so the
  published `build_command` and the Snakefile's DAG would describe a machine rather
  than a release.
- **No race conditions at planning time.** A file can appear or be deleted between
  the plan and the execution. Checking at planning time would freeze a stale answer
  into a plan that is then executed later; checking at execution time asks the
  question at the only moment the answer is meaningful.
- **The DAG edge survives a skip.** Because the pre-stage is always planned, the
  build step's declared input always names the artifact and Snakemake always
  sequences extraction before the build. A skipped step still writes a record, so
  the record chain the `{step}` rule and `register` walk is unbroken.

## Why existence is a runtime fact

Whether a shared panel is already on the host is operational state. The same
bundle must be buildable on CI, on a developer laptop, and on the production host,
and only the production host may already hold a multi-hour reference. Encoding the
answer in the bundle would make the bundle non-portable; encoding it in the plan
would make the plan wrong on whichever host it was not computed for. The executor
is the single place that can see both the plan and the host, so the decision lives
there.

## Consequences

- The planner carries a declared pre-stage the same way it carries every other
  step: as argv and explicit inputs/outputs. The workflow needs one new step name;
  the rules, the record chain, and `register` need no structural change.
- A skipped extraction is visible in the release evidence rather than implied by
  a missing file, so an auditor can distinguish "extracted during this run" from
  "provided by the host".
- The bounded option-key cross-check is the only place a `build.options` value is
  read rather than rendered. Any future proposal to interpret another option key
  must clear the same bar: two renderings of one fact must not be allowed to
  disagree, and the check must stay syntactic.
