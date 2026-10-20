# PROTOTYPE — analysis catalog for the multi-store API (throwaway)

```
python3 prototypes/analysis-catalog/tui.py        # or: pixi run prototype-catalog
```

Reads the real registry (`stores.tsv`, `stores/OGS-*/release.yaml`, `stores/OGS-*/analyses.tsv`)
and the real built Stores' `analyses.tsv` under `/data/opengwasdb/stores/` **read-only**, then
holds everything in memory. Nothing is written. Needs a terminal ~160 columns wide.

## The question

opengwasdb#93 needs "multi-store catalogue and routing behaviour defined": R/Python clients and
the website must list every Analysis across every Store, and each query must land on one Store.

The model under test: the catalog is **not a database that gets updated**. It is a pure fold,

```
make_fragment(store, bundle_rows, built_rows) -> Fragment   # per Store Release, re-derived only when it changes
build_catalog(fragments, policy)              -> Catalog    # always a full fold over every fragment
diff(old, new)                                -> Diff       # what a client would see change
```

in which one **Analysis** (identity key) has several **copies** (one per Store that physically
holds it), and a **serving policy** picks the default copy and records why. Does that model give
the right answer through the registry's real lifecycle events?

`catalog.py` is the model and could be lifted out as-is. `scenario.py` (seeding, events) and
`tui.py` are throwaway.

## What to try

| key | event | what it tests |
|---|---|---|
| `1` | build MVP OGS-00012..16 | a new Store arrives: +6,022 Analyses, and every one of them has no `analysis_label` |
| `2` | rebuild OGS-00011 from its #203 bundle | bundle/disk membership drift resolves; clients see nothing because policy already hid the 148 |
| `3` | registry status catches up | under `servable_by=disk` this is invisible; under `status` it lists ~8k Analyses |
| `4` | erratum on OGS-00017 | bundle-only metadata fix reaches clients without a rebuild |
| `5` | supersede OGS-00006 | 1,400 defaults move to the new release; the old copy stays resolvable |
| `6` | withdraw OGS-00009 | its completed child OGS-00010 is derived from it; toggle `w` to see both answers |
| `7` | add a GTEx whole-blood eQTL store | `ENSG…::whole_blood` collides with eQTLGen under `identity=analysis_id` |
| `8` | add a GWAS Catalog store re-serving 40 MVP GCSTs | the same study from two providers should be one Analysis |
| `s t c i w` | policy toggles | servable_by, trial, completion preference, identity key, withdrawal propagation |
| `b` | ×100 synthetic scale | full fold over 1.75M copies / 1,900 Stores |

## What the real data showed (2026-10-21)

- **A built Store's `analyses.tsv` is not the current metadata.** OGS-00001 still carries the
  `ENSEMBL:` terms erratum #141 removed. OGS-00006's Store has no `original_sd`, while its bundle
  has it for all 1,400 rows. The bundle has to be overlaid onto the Store.
- **Bundle membership ≠ built membership.** OGS-00011's Store holds 148 Analyses its re-emitted
  bundle excludes (`not_maf_filtered`, #203).
- **Release Status does not track the disk.** 08–11 and 17 (10,129 copies) are built but
  `candidate`/`accepted`, and no `publication.json` exists. A status-driven catalog lists 1,400
  Analyses; a disk-driven one lists 9,347.
- **Nothing records Trial-ness.** Only the label ("pilot") does, so `trial` is inferred here.
- **`analysis_id` is not a global key.** Every overlap today is lineage or trial→full, but
  `gene::tissue` IDs will collide across eQTL sources, and MVP reuses GWAS Catalog accessions.
  Keying on the *issuing authority* (`gwas-catalog:GCST…`, `<collection>:<id>` otherwise) gets
  both cases right; neither bare `analysis_id` nor `collection:analysis_id` does.
- **MVP bundles (OGS-00012..16) have no `analysis_label`**, only `source_label`.
- **Copies of one Analysis disagree** on interpretive fields (e.g. `assigned_ancestry` between
  OGS-00003 and OGS-00017), so a listing shows the default copy's metadata, not a merge.
- **Full rebuild is cheap.** Today's fold takes 55–75 ms. At ×100 it takes 7.6 s in pure Python.
  Per-store fragments are 20–40 ms each.

## Verdict

_Pending the owner driving it._ Open decisions the prototype surfaces: withdrawal propagation
down lineage; default completion preference; which provider wins when one study is in two
collections (currently "newest").
