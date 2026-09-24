"""Curation tooling for OpenGWASDB Store Releases.

This package holds repository-owned curation stages that operate *over*
committed Release Manifests without changing them. The first such stage is the
gap scan (:mod:`curation.gap_scan`), which derives the unmapped Trait work queue
that feeds Canonical Trait Mapping Table curation.
"""
