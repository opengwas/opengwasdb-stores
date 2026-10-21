# HM3 release benchmark report tests (stores #212, epic #214)

Executable proof that the release-side report
(`resources/scripts/release-benchmark-report.py`) passes the core HM3 statuses
through unchanged:

1. a missing or partial `--opengwasdb-checkout` is refused with the missing
   pieces named;
2. `missing`, `not_applicable`, `unsupported`, `not_queryable` and `no_anchor`
   are recorded without timing keys and never become shape errors;
3. a `measured` record's timing survives;
4. the generated Markdown states that HM3 benchmark absence is operational
   evidence, not a Store validation failure;
5. the first `analysis_id` anchor is located by column name.

The tests use a stub core checkout, so they never open a multi-gigabyte Store.
`pixi run test` discovers and runs them.

The real report is generated against an explicit core checkout so its revision
and fingerprint are recorded; the tracked runtime now provides the capability
(stores#218):

```bash
pixi run -e dev python <stores>/resources/scripts/release-benchmark-report.py \
  --opengwasdb-checkout /path/to/opengwasdb \
  --all --hm3-only \
  --output docs/benchmark-output/opengwasdb_hm3_release_benchmark.json \
  --report docs/benchmark-output/opengwasdb_hm3_release_benchmark.md
```

The JSON artifact and the Markdown report are generated; re-run the command
rather than editing either.
