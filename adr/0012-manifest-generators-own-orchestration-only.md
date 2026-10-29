# Manifest generators own orchestration only

Manifest Generators may live in this repository when they perform Store Family-specific discovery, selection, prioritisation, and manifest emission. Reusable source readers, normalisation logic, and store build engines belong in OpenGWASDB or another shared package rather than being reimplemented inside the registry.

**Amended by [0028](0028-store-family-tier-retired.md).** "Store Family-specific" is historical vocabulary: the Store Family record tier is retired, but Manifest Generators still live in this repository and their directories keep their historical slugs (`resources/generators/<family-id>/`). The decision stands per Manifest Generator.
