- The release benchmark is measured for OGS-00001, OGS-00003 and OGS-00007 at
  format 0.3.0 (issue #514) on a quiet host: 1-minute load 5.8 before and 5.7
  after, peak RSS 0.75 GB. The results are in
  `docs/benchmark-output/opengwasdb_514_canaries_benchmark_2026-11-25.{json,md}`.
  OGS-00003's HM3 extract takes a median of 385 ms; the two Ragged Stores are
  `not_applicable`. New anchors for OGS-00001 and OGS-00007 are in
  `release-benchmark-anchors.yaml`.
