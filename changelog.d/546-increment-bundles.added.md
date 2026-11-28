- **Increment Bundles change a published Hybrid's membership under its Store id
  (issue #546, ADR 0045).** `stores/<id>/increments/<nnnn>/` declares the
  Analyses an increment adds and the ones it drops, with the owner's acceptance
  bound to the reviewed ids and the increment's `content_sha256`, and an
  owner-attested `source-record.json` (`pixi run increment-source-record`).
  `bundle-check` holds every increment to the contract.
  `python3 -m manifest_generation.emit <id> --increment` and
  `generate-candidate --increment --selection <roster>` write one without
  touching any other bundle file, reconciled to an explicit roster (every
  listed id admitted or refused at resolution, nothing else) and only under the
  bundle's own release config and resolver settings. `ogstores.increment.fold` is the fold the increment route (#560) will
  apply at publication. The amendment check holds the published `work/` files
  to the sha256 the last published increment recorded, accepts exactly a
  declared increment (`amendment.increment_changes`), and still refuses an
  undeclared membership change, now pointing to an Increment Bundle.
