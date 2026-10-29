# Retain full normalised long alleles

Canonical variants are trimmed and left-aligned before identity is assigned. Long alleles may be represented compactly by a deterministic hashed ALID, but the Store retains the complete normalised alleles once per variant.

The hash is a compact identifier and lookup key, not the authoritative representation. Exact export, validation, and harmonisation must not depend on an irreversible hash.

