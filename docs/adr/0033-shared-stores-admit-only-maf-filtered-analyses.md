# A shared Store admits only MAF-filtered Analyses

`OGS-00011` is the shared Hybrid Store of 3,317 GWAS Catalog Analyses. Its
shared variant axis is 164,051,296 variants; 150.6 M of them are off-panel
(Ragged Overflow) against a 9.85 M-variant EUR panel, on 3.09 B Overflow rows.
Per-variant structures over that axis are large on disk:
`variant_alid_bytes.npy` 5.2 GB, `variant_offsets.npy` 1.31 GB, the by-variant
index offsets 1.31 GB, `eaf_baseline` 0.66 GB, `variant_alid_rows.npy` 0.65 GB.
Build and validation time and memory scale with the axis (opengwasdb #254), and
any path that walks it does too. Open-time memory does not: after opengwasdb
#252 the Store opens in about 0.29 GiB.

`config-full.yaml` sets `defaults.maf_threshold: 0.005` and exempts
`Whole genome sequencing` and `Exome-wide sequencing`
(`source.maf_filter_exempt_genotyping_technologies`, operator decision, #176):
their low-frequency calls are observed, not imputed. 148 included Analyses
therefore carry no MAF floor: 97 WGS and 51 WES. Those 148 alone carry
49,537,183 Overflow variants (32.9 % of the Overflow) on 63,770,673 rows (about
2 % of Overflow rows). 49.3 M of the axis's 75.9 M variants below 0.5 % MAF are
carried only by them; WES-only variants are 76,409.

Issue #203 called these 148 "no-EAF" Analyses because the Store provenance
records their `maf_state` as `unavailable`. That is a misnomer: they do report
EAF. Every one passed the EAF-orientation check, 126 had their phenotype SD
estimated from source MAF, and a sampled WGS source (`GCST90446475`) carries
`effect_allele_frequency` on every one of its first 2 M rows, 81 % of them below
0.5 % MAF. `unavailable` is the resolver's state for "no floor was requested",
not a statement about the source. The rule is therefore worded as "rows were
MAF-filtered", not "the source reports EAF": a no-EAF rule would admit all 148.

The decision recorded on #203 adopts levers A (split Stores by genotyping
provenance) and B (an Analysis whose rows cannot be frequency-filtered cannot add
off-panel variants to a shared Store). It does not pursue C (extend the Dense
panel to widely shared variants) or D (raise the MAF floor to 1 %).

## Decision

A shared Store admits only Analyses whose rows were MAF-filtered. The rule is
opt-in per generator config, through a new Phase B block:

```yaml
store_composition:
  require_maf_filtered: true
```

Absent means `false`. The block must be a mapping whose only key is
`require_maf_filtered`, a real boolean. `true` requires a positive
`defaults.maf_threshold`: with no floor, or a disabled zero floor, no Analysis
can be MAF-filtered, so configuration fails rather than admitting everything.

When the rule is on, the candidate workflow's release policy runs one more gate
**after every other membership decision**. An Analysis that would otherwise be
included but whose emitted `maf_threshold` is literal `NaN` (no MAF floor applied
on resolver evidence) is excluded with reason `not_maf_filtered` and sidecar
category `store_composition`. The exclusion detail records the requested
`maf_threshold`, the resolver's `maf_state`, the source metadata's
`genotyping_technology`, and how many of its build-eligible rows are off the
variant reference. An Analysis excluded for any other reason keeps that reason,
so `not_maf_filtered` means "admissible except for Store composition" and never
masks a resolution, ancestry, orientation, or metadata failure.

Like `effect_placeholder_rows`, this is an **emit-time membership decision**: it
does not change the resolver manifest or the resolution receipt's contract, so
toggling it needs only `--stage emit`, never a re-resolve.

Every candidate `validation.yaml` carries a top-level `store_composition` block
recording what the gate saw:

- `require_maf_filtered` (bool): whether the rule ran;
- `maf_floor` (string or null): the configured `defaults.maf_threshold`;
- `unfiltered_included` (int): included Analyses with no applied MAF floor;
- `unfiltered_included_off_reference_rows` (int or null): the sum of their
  build-eligible rows off the variant reference;
- `unfiltered_routed` (int): Analyses excluded as `not_maf_filtered`;
- `unfiltered_routed_off_reference_rows` (int or null): the same sum for them.

A sum is `null` when no `variant-reference` is declared or when any counted
Analysis lacks the resolver count: absence is never summed as zero.

Two warnings become `Review:` lines in `release.yaml` notes and appear in
`validation.yaml` `warnings`: one when Analyses were excluded as
`not_maf_filtered`; and, when the rule is off but a MAF floor is configured, one
when included Analyses have no applied floor -- "their off-reference rows enter
the shared variant axis unfiltered". That second warning is what stops a later
release from silently re-inflating the axis.

When the rule is on, `release.yaml` notes gain the line "Store composition
(#203): this shared Store admits only Analyses whose rows were MAF-filtered; an
included Analysis with no applied MAF floor is excluded as not_maf_filtered."

`register` (`src/ogstores/register.py`, `acceptance_evidence`) carries the
candidate record's `store_composition` block into the `acceptance` block by name,
alongside `checks`, `warnings`, `reports` and `reference_overlap`.

## Rejected alternatives

**Key the rule on EAF presence** (the issue's original wording). It would admit
all 148, because they report EAF; the evidence above is that a missing floor, not
a missing frequency, is what lets their variants inflate the axis.

**Key the rule on the genotyping technology** (route WGS only). Technology is the
reason a floor was waived, not the property that inflates the axis. It would keep
the 51 unfiltered WES Analyses, and it would route WGS-plus-array Analyses that
were filtered; and missing technology metadata would decide membership.

**Withdraw the sequencing MAF exemption** (filter WGS/WES at 0.5 % and keep
them). It discards the observed rare variants the exemption exists to keep, and
it changes the resolution contract, so all 4,783 Analyses would need
re-resolving.

**Admit unfiltered Analyses on-panel only** (lever B's other form). It needs a
per-Analysis row filter in the opengwasdb builder that does not exist, and would
store a partial Analysis that looks complete.

**Levers C and D** (extend the Dense panel to widely shared variants; raise the
MAF floor to 1 %). Out of scope by decision on #203.

## Consequences

The 148 leave `OGS-00011`'s next build. They need their own sequencing Store, a
separate Store Release not yet made; that is follow-up work, and no `OGS-` id is
assigned here. The axis is expected to shrink by about the 49.5 M variants only
they carry; the rebuild measures it.

The rule is opt-in per generator config, and with it off the second warning keeps
unfiltered Analyses visible rather than letting them re-inflate the axis
silently.

See #176, #203, opengwasdb #252 and #254, and
[ADR 0032](0032-phase-b-candidate-workflow.md).
