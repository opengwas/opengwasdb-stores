# Workflows

Two workflows, deliberately separate, meeting at the accepted Release Bundle.

| | | |
|---|---|---|
| `Snakefile` | Phase A | accepted bundle -> validated Store Release |
| `generate.smk` | Phase B | raw sources -> candidate bundle (not yet designed) |

They share no DAG. The reason is not that one graph would be complex: the
accepted bundle is a boundary *only because a human froze it*. Span both
phases with one DAG and Snakemake will correctly, silently, regenerate a
bundle and rebuild a 71 GB Store because a generator config changed upstream,
and the acceptance gate stops existing.

See `docs/spec/store-release-workflow.md`.
