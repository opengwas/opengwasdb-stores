# Registry and service catalogue boundary

This repository produces registry artifacts that a future service catalogue can ingest, including Store Families, Release Manifests, Build Recipes, validation reports, Release Lineage, Release Errata, Trait Annotations, and build priorities. Runtime service concerns such as request routing, authorisation enforcement, billing, quotas, default served releases, and usage audit logs belong in the service catalogue layer rather than this repository.

**Amended by [0028](0028-store-family-tier-retired.md).** Store Families and build priorities are both retired as registry artifacts: the `OGS-` Store Release is the unit, `access_posture` is descriptive `release.yaml` metadata, and prioritisation lives in the issue tracker. The service-catalogue boundary itself is unchanged.

**Amended by [0039](0039-analysis-catalog.md).** The service-catalogue layer this ADR reserved is now the Analysis Catalog, and the serving decision is built there: the catalog reads Release Bundles and lists the Store Releases to serve, so the registry still records no serving decision. Request routing, authorisation enforcement, billing, quotas and usage audit remain outside the registry and outside the catalog.
