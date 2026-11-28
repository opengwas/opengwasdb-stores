# Store release identity

A new Store Release is required when the accepted Release Manifest changes or when material Build Recipe choices change the produced analytical asset. Curating descriptive Trait Annotations for studies already present in a release does not by itself create a new Store Release, because it changes the interpretive registry metadata rather than the immutable store contents.

**Amended 22 November 2026 (issue #478), a one-off exception before the first
release.** On the owner's decision of 2026-11-22, OGS-00011 is re-emitted and
rebuilt under its own id although its manifest changes (3,169 to 3,245
Analyses) and its Build Recipe moves to the reference-first gnomAD Variant
Reference. Its accepted bundle went from `validated` back to `candidate`, a
transition the Release Status lifecycle does not otherwise allow
(`bundle.LEGAL_STATUS_TRANSITIONS`), so that emit could replace it. Its release
notes carry the change as a Release Erratum, and the readiness ledger records
that the committed bundle does not describe the published Store until the
rebuild publishes. Like #326's and #409's in-place changes (CONTEXT.md, Store
Release), it sets no precedent after the first release; the rule above stands.

**Amended 28 November 2026 (ADR 0045, issue #546).** On the owner's ruling of
2026-11-28 (Gibran Hemani, recorded on opengwas/opengwasdb#508), a Store id
names a sequence of Store Releases, each identified by its `release_id`. An
accepted Increment Bundle that adds Analyses to a Reference-Completed Hybrid,
or removes them for a defect, is published by splicing (opengwasdb ADR 0109)
as a new Store Release under the same id. The previous release is archived
under `replaced/<UTC>/`, and a defect removal is also a Release Erratum
(ADR 0005). Only that change keeps the id, and only under the conditions in
ADR 0045 §1. Every other change to the Release Manifest or to a material Build
Recipe choice still needs a new Store id, under the rule above. Here a "new
Store Release" in that rule means a new Store id, never only a new
`release_id`.
