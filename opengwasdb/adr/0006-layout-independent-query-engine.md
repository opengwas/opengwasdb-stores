# Use a layout-independent query engine

The public query API is layout-independent from the start. Callers query a Store Release for variants, ranges, analyses, full analyses, or top hits without knowing whether associations are stored in Dense, Ragged, or Dense-plus-Overflow form.

Layout-specific implementations live behind internal adapters selected from the store manifest. This keeps Dense-first implementation work from leaking dense-specific assumptions into the API and allows Ragged and Reference-Completed stores to share the same result contract.

