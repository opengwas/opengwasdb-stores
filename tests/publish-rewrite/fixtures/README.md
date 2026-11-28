# `dense-0.2.0.tar.gz`

A real format-0.2.0 Dense Store Release, 201 variants by 2 Analyses (384 KB
unpacked, 82 files), for the one test that runs `opengwasdb rewrite-release
--convert-legacy` and `ogstores.publish_rewrite` with the real engine
(`TestRealEngine`, #533). Store contents are tested in opengwasdb; this
fixture exists to prove the route against the engine's real output.

Built on 25 Nov 2026 with engine `c8ebe4ca` (0.20.0) and that commit's own test
helpers, as its #492 tests build their 0.2.0 sources:

1. `store_reader_digests.build_releases()["dense_observed"]`, then
   `format_0_2_3.as_0_2_3` and `format_0_2_0.as_0_2_0`;
2. `format_0_2_0.plant_se` on `a_top_hit`'s cell, SE 1e5, so the conversion
   masks and counts one cell;
3. the EAF orientation recorded as `passed` for both Analyses, in
   `analyses.tsv`'s `eaf_orientation` and in `manifest.json`'s
   `provenance.eaf_orientation.analyses[].outcome`, so the release raises no
   native warning (`eaf_orientation.unverified`);
4. `manifest.json`'s `store_id` set to `OGS-00042`, the test's id.

Converted `--lifted`, it validates with `variant_table.source_alid`, a recorded deviation, and `effect_consistency.unchecked` (#510), which the route accepts as a named legacy warning (`publish_rewrite.LEGACY_WARNINGS`), as the replaced record carries none. Its variants sit at chr1 10,001-10,600, on sequence: its first build, at 1:1-1:600, was in chr1's leading assembly gap, which opengwasdb#410 drops. Patched in place on 28 Nov 2026 (positions +10,000; `variants.tsv.gz`, its index and the ALID and offset sidecars rewritten).
Packed with every mtime 0.
