# Use explicit local store paths and keep catalogues external

The Python package directly queries downloaded or otherwise local Store Releases. Build and query CLI commands operate on explicit Store paths supplied as arguments.

Remote querying, store discovery, default-release selection, directory naming conventions, and multi-store organisation belong to a separate catalogue or deployment layer, not the v0.1 Store contract.

