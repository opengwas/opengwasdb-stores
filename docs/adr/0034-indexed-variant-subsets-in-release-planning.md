# Indexed Variant Subsets are optional, declared in the Build Recipe

Epic opengwas/opengwasdb-stores#214 turns the HapMap3 extraction experiment
(opengwas/opengwasdb#262, implemented in #263/#269) into routine release
infrastructure. opengwas/opengwasdb ADR 0061 defines a named **Indexed Variant
Subset** as an optional, rebuildable Dense-store query index over a
caller-supplied list of canonical ALIDs, built by:

```text
opengwasdb build-indexed-subset STORE SUBSET_NAME \
  --variant-list hm3.alid.txt --reference-assembly GRCh38
```

It is a non-authoritative derived artifact stored under
`data.zarr/indexed_subsets/<name>/`; removing it leaves a valid release. A
Compact release has no Dense Component, so it gets no subset. A Hybrid release's
subset lives on its self-contained Dense Component.

This registry must plan and run that step for a release that declares it. The
capability first ships in opengwas/opengwasdb `4819a2ee` (opengwas/opengwasdb
#264), which is a descendant of the format-0.2.0 builders (opengwas/opengwasdb
#240). The repository pin (`7b0c42d`) deliberately predates both, and adopting
format 0.2.0 catalogue-wide is the separate migration tracked by stores#197.

## Decision

A Build Recipe may declare one or more optional named subsets in a top-level
`indexed_subsets` list:

```yaml
indexed_subsets:
  - name: hm3
    resource_id: hapmap3-hg38
    variant_list: /data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt
    reference_assembly: GRCh38
    checksum: 4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398
    options: {band-cells: 65536000}
```

- `name` (required) is the subset identity, using the consumer's own
  `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` grammar and additionally refusing `..`.
- `resource_id`, `variant_list` and `reference_assembly` (required) name the
  maintained variant-list Reference Resource (stores#208) and the concrete path
  and assembly the build uses. Recording the resolved path keeps the immutable
  bundle self-contained, and `plan()` needs no resource I/O.
- `checksum` (optional) is the maintained list's sha256, cross-checked against
  the resource declaration at run time.
- `options` (optional) is `opengwasdb` flag passthrough for the subset command;
  `overwrite` is refused because a release declares a named subset to build, and
  so is any key that normalises to `variant-list` or `reference-assembly`, since
  those are the declaration's own verified inputs and re-specifying one would
  let the executed CLI use a value the runtime never checked. The key is
  optional; omitting it declares none, while an explicit `indexed_subsets: null`
  is malformed rather than silently treated as omitted.

`plan()` emits one `build-indexed-subset` Step per declaration, in declaration
order, after the Store and its inline top-hit/rho steps exist and before
`overview` and `validate`, so overview reports the subset and validation audits
it. The Step is an ordinary Step, so its durable `records/indexed-subset-<name>
.json` carries planned and executed argv, inputs, outputs, stdout/stderr,
timing, success and the `opengwasdb` revision used, with no new record schema.
`register` already requires a record for every planned Step and compares
executed against planned argv.

A Ragged release rejects an `indexed_subsets` declaration at plan time and in
`bundle.check()`. It is never silently ignored: the epic's non-goal is that
Ragged releases get no HM3 subset, and CONTRIBUTING's first rule is to fail
loudly rather than skip a declaration. A recipe with no declaration, or an empty
list, plans byte-for-byte as before.

## Runtime prerequisite, and the pin is held

The capability is a **run-time prerequisite**, not a tracked dependency change.
Before a run that declares a subset, `ogstores.release` (and the Snakefile
`onstart` hook, for a direct `snakemake` run) probes the resolved `opengwasdb`
for `build-indexed-subset` and refuses with the required capability if it is
absent. It also resolves the declaration's `resource_id` under
`resources/reference-resources/<id>/resource.yaml` and hashes the list against
the maintained sha256, byte size and row count. `plan()` stays pure: it only
renders argv.

This has two honest consequences:

1. **A subset-declaring release runs the whole workflow on a capability
   runtime**, because `validate`/`overview` must audit and report the namespace
   and because no opengwasdb revision has the subset feature without the format
   0.2.0 builders. That release is therefore format 0.2.0 and its Validation
   Record names the actual runtime commit, not the tracked pin. This is opt-in
   per release; no existing 0.1.0 Store is touched and there is no bulk
   migration.
2. **The pinned `opengwasdb` stays `7b0c42d` until stores#197.** A run that
   declares no subset is never gated on the runtime version, so every existing
   recipe and every no-declaration plan is unaffected.

## Amendment (stores#218): the pin is now the current-format runtime

stores#218 adopted the runtime portion of stores#197: the tracked pin is now
`4e922566d7fa4c13a11be3637069da63e48be28c` (core `dev`, repinned from the initial
#218 pin `1be2a15` when core #254 merged). It carries the
format-0.2.0 builders, the required Ragged/Overflow `by_variant/` index, the
Indexed Variant Subset capability and the shared HM3 benchmark seam. The
decision above that the subset capability is an opt-in *run-time* prerequisite
still holds, and the pre-run probe and resource hashing still run unchanged;
what changes is consequence 2: the pinned runtime now provides the capability,
so no `PATH` overlay is needed and a supported run never substitutes one. See
[docs/runtime.md](../runtime.md).

The guard was generalized with the pin. `ogstores.runtime.verify_runtime_for_releases`
now also refuses a run whose resolved executable's identity is unprovable or is
a revision other than the pin, whose scheduled steps name a subcommand the
runtime lacks, or which would complete an existing parent Store that is not
format 0.2.0. The parent gate is scheduled-step-aware: it applies only when the
child's own `complete` job will run, skips a stale parent when a same-run
`build`/`complete` of that parent is scheduled, and refuses a missing parent
that is not scheduled to be produced. Rejected option 3 above is superseded by
this ticket: the format adoption is now deliberate and pinned rather than
deferred, and it still does not convert, rebuild or requalify any published
Store. A format-0.1.0 parent is refused and must be converted or rebuilt before
Reference Completion, following the documented procedure in
[docs/runtime.md](../runtime.md) rather than an ad-hoc script contract.

## Considered options

- **Resolve the resource inside `plan()`.** Rejected: `plan()` is a pure
  function with an executable tripwire test; opening `resource.yaml` or the list
  would break the planner contract the golden-argv suite protects. Resolution
  and hashing live in the run-time preflight instead.
- **Declare only `resource_id` and resolve it at plan time.** Rejected for the
  same purity reason; recording the resolved path also keeps the immutable
  bundle self-contained and reviewable.
- **Bump the pin in this ticket.** Rejected: every subset-capable revision also
  changes the Store format to 0.2.0, so a pin bump would silently adopt
  stores#197's catalogue-wide migration. That decision belongs to #197.
- **Silently ignore a Ragged declaration.** Rejected: a declared subset that is
  not built is a wrong answer that looks like a right one.
- **A separate subset index store.** Rejected by opengwasdb ADR 0061: the index
  is a derived artifact under the release it accelerates.
