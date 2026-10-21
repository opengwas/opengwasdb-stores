# Reference completion as release lineage

Reference-completed stores are normally represented as Store Releases within the same Store Family as their observed-only input, linked by Release Lineage. This keeps source and product identity stable while allowing completion reference panels and completion parameters to vary as release-level build choices.

---

**Amended by [0028](0028-store-family-tier-retired.md).** Store Releases are no longer grouped in a Store Family; Release Lineage is the `derived_from` edge alone.

**Superseded by [0037](0037-reference-completion-replaces-its-store.md).** A reference-completed Store keeps one id: its completed Store replaces its Observed-Only Store under that id, rather than becoming a second Store Release derived from it. Existing two-id pairs, such as OGS-00001/OGS-00002 and OGS-00009/OGS-00010, keep their `derived_from` lineage.
