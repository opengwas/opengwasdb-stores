"""Bounded MVP metric-resolution pilot: metadata-only selection (#221).

This package builds the reviewed 15-Analysis pilot manifest and runs the public
OpenGWASDB resolver once under enforced resource limits. It is fixture-first:
`plan` is the default and performs metadata-only work; `run` requires an explicit
`--execute`, the approved immutable metadata SHA and a parent start receipt.

No module here opens an association body. The resolver (a public core CLI run by
`wrapper`) is the only component that reads source files, and it is pointed at an
allowlisted set of paths under a preflighted, fresh output root.
"""

from __future__ import annotations

__all__ = ["selection", "wrapper", "cli"]
