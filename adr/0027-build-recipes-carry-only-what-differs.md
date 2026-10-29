# Build Recipes carry only what differs between Store Releases

A Build Recipe exists to show a reviewer the release's real choices at a glance,
and to make authoring a new one a small job. A key restated with the same value
in every Release Bundle buys nothing and buries the choices that actually vary.

At the time of this decision, seven accepted Release Bundles (OGS-00001..00007)
restated two `post` keys identically:

```text
rho       false in 7/7
validate  true  in 7/7
```

`top_hits` and `overview` varied. `overview` in particular became a real
per-layout choice in commit 6092fee: Dense and Hybrid releases enable it, while
Ragged releases reject it because their closed Store envelope excludes
`overview.html`.

## Decision

A `post` key that is genuinely identical across every committed Release Bundle
becomes a default in `plan()` (`POST_DEFAULTS`) rather than a value every recipe
restates. A recipe states only the keys it chooses differently from the
defaults, and an explicit value in the recipe still wins.

A key may default only when it is constant across *every* bundle. `top_hits` and
`overview` therefore stay explicit. Defaulting `overview` on would silently
re-enable an overview step on the four Ragged releases that deliberately reject
it -- the planner would then fail with an error the recipe did not ask for.

## Consequences

- Every committed Build Recipe loses `rho` and `validate`; a new recipe omits
  them unless it is overriding the default.
- `plan()` output, and therefore the golden argv and the master list's
  `build_command`, is byte-identical: this is a representation change, not a
  behaviour change.
- The default set lives in one place. Adding a key to it is a decision that
  requires the same justification this ADR records: the key must be constant
  across every Release Bundle.
