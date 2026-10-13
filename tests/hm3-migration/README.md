# HM3 Indexed Variant Subset migration (#211)

Executable proof for stores#211 (epic #214), the registry-side migration that
makes the `hm3` Indexed Variant Subset the default Dense-component artifact
across the existing registry. The audit script lives at
`resources/scripts/audit_indexed_subsets.py`; this suite tests it and the
migrated bundles.

## What was migrated

Every registered Release Bundle with a Dense Component — a `dense` or `hybrid`
`layout` — declares exactly one top-level `indexed_subsets` entry:

```yaml
indexed_subsets:
  - name: hm3
    resource_id: hapmap3-hg38
    variant_list: /data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt
    reference_assembly: GRCh38
    checksum: 4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398
```

The eight in-scope bundles are `OGS-00003`, `OGS-00004`, `OGS-00005`,
`OGS-00008`, `OGS-00009`, `OGS-00010`, `OGS-00011` and `OGS-00017`. The four
Ragged-only bundles (`OGS-00001`, `OGS-00002`, `OGS-00006`, `OGS-00007`) are
byte-for-byte untouched: a Ragged Store has no Dense Component to index.

The subset is an optional derived artifact (opengwas/opengwasdb ADR 0061). Adding
it changes no Analysis, Analytical Metadata or Store validity, so it mints no
new Store Release: `release.yaml` status, lineage and `analyses.tsv` are
unchanged, and no primary data is rebuilt. The only bundle change is the added
`indexed_subsets` key; every builder option and `post` value is unchanged.

The maintained list is rebuilt against a Store axis, so `hapmap3-hg38` is
derived from the OGS-00009 GRCh38 axis and the same ALID list is applied to
every migrated Dense component; a Store axis that lacks some ALIDs is the
ordinary "absent from the axis" case the builder counts and records.

## The audit

`resources/scripts/audit_indexed_subsets.py` prints the full in-scope list and
fails (non-zero) when:

- a Dense-component bundle has no `indexed_subsets` key and no explicit opt-out;
- a Dense-component bundle declares subsets but not `hm3` and has no opt-out;
- a Ragged-only bundle declares any subset;
- a declaration does not statically match the maintained `hapmap3-hg38`
  resource (`resource_id`, `kind`, `location`/`variant_list`,
  `genome_build`/`reference_assembly`, `checksums.alid_sha256`/`checksum`).

It reads only `build.yaml` and the small `resource.yaml`; it never opens the
18.9 MB variant list, a Store, or an association body, so it is CI-safe where
the reference root is absent. The exhaustive byte/row-count check remains the
runtime preflight (`ogstores.resources`) and the resource's own
`verify_hm3_resource.py`.

An intentional opt-out is a present, empty `indexed_subsets: []` — a reviewable
statement in the bundle that distinguishes "declared none" from "not migrated".
An absent key on a Dense-component bundle is a loud miss. stores#210 owns the
generator-facing default/opt-out; if it adopts a different marker, this audit
must adopt that marker too.

```sh
pixi run python resources/scripts/audit_indexed_subsets.py
pixi run python tests/hm3-migration/test_hm3_migration.py
```

## Scope limitation

The unmigrated `families/*/releases/*` bundles are **not** audited. They use the
retired family-first schema (`store_family_id`, `store_layout`,
`builder.entrypoint`) that `bundle.check()` and `plan()` reject, carry no
`OGS-` id, and are not planned by the workflow. stores#211 is registry-only;
those bundles get an `indexed_subsets` declaration when they are migrated to
`stores/<store-id>/`, which is separate work (ADR 0028). The audit prints this
limitation so an empty failure list is not mistaken for coverage of them.
