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
