# Project site data

The specification of the project site's data (#389, epic #385): the **epic
record** an orchestrated epic commits when it finishes, and the **collectors**
`pixi run site-data` runs to gather everything else the site's pages read. The
pages themselves (#391) and the site build (#388) are specified elsewhere; this
file is the contract between the data and them.

The code is `src/project_site/` (ADR 0038). It imports `ogstores.bundle` and
`ogstores.paths` and nothing else from this repository, and it reads
`/data/opengwasdb/stores` only through read-only SQLite URIs.

## The epic record

One YAML file per epic, `docs/_epics/<repo>-<number>.yaml`, named for the
epic's repository and issue number. The leading underscore keeps Quarto from
rendering it. The orchestrator writes it from the run's `state.json` when the
epic finishes; an agent writes it in one pass, because every field is a scalar,
a list of flat rows, or a mapping of two keys.

**An unknown value is `null`, never an estimate.** Every field is required, so
that a field left out is distinguishable from a field not known. A date, a
number or a reference that is not the real thing is refused, and so is a text
that reads as a guess: empty, `unknown`, `tbd`, `n/a`, `none`, `?`, anything
starting `~`, or starting `about`, `approx`, `around`, `circa` or `est`.

```yaml
repo: opengwasdb-stores            # the epic's repository, without the owner
number: 313                        # the epic's issue number
title: Analysis catalog across all Stores
status: active                     # active | finished
workstream: Analyses               # a board Workstream, or null
started: 2026-10-21                # ISO date, or null
finished: null                     # ISO date; null exactly while active
summary: >-                        # 2 to 4 sentences, or null
  The Analysis Catalog lists every validated Store. ...
prs:                               # repo#n keys, in merge order
  - opengwasdb-stores#353
versions: []                       # tags the PRs shipped; empty until the cutover
tickets:
  - key: opengwasdb-stores#319     # repo#n, or null for work with no issue of its own
    title: analysis_id never contains ':'
    pr: null                       # repo#n, or null
    merge: 7832deb                 # 7 to 40 hex commit on the integration branch, or null
decisions:
  - date: 2026-10-21
    text: Integration branch cut from dev, not stacked on #299.
    link: null                     # http(s) URL, or null
test_time:                         # the full suite, or null when never measured
  baseline: {commit: e6e829d, wall_s: 158.4, suites: null}
  final: {commit: a84ecfb, wall_s: 162.05, suites: null}
yardstick:                         # paths under docs/_yardstick/ (#390), or null
  before: null
  after: null
catalog:                           # Analysis Catalog versions, 16 hex, or null
  before: null
  after: 1b3a155d449bb84a
followups:
  - key: opengwasdb#294            # repo#n, or null
    text: builders print n_associations
```

Rules `validate_epic` applies, and the order of the fields above:

| Field | Rule |
|---|---|
| `repo`, `number` | a repository name; an integer of 1 or more. The file name is `<repo>-<number>.yaml` |
| `title` | text, never null |
| `status` | `active` or `finished` |
| `workstream` | one of Data architecture, Build architecture, Interface, Store builds, Tool integration, Analyses, Engineering, or null |
| `started`, `finished` | an ISO date (`2026-10-21`, unquoted or quoted) or null. A finished epic has a `finished` date; an active one has none |
| `summary` | 2 to 4 sentences, or null |
| `prs`, `tickets[].key`, `tickets[].pr`, `followups[].key` | `repo#n`: a repository name, `#`, an integer of 1 or more |
| `versions` | tag names. Empty until each repository merges to `main` and tags every merge (#386) |
| `tickets[].merge` | a hex commit of 7 to 40 characters, or null. A ticket with no recorded merge commit is null, never its PR's head |
| `decisions[].date`, `.text` | an ISO date and text, both required; `link` is an `http(s)` URL or null |
| `test_time.baseline`, `.final` | null, or `{commit, wall_s, suites}`: a hex commit or null, seconds as a number or null, and `suites` null or a mapping of suite name to seconds. A run state records one total, so `suites` is null for every backfilled record |
| `yardstick.before`, `.after` | a relative path under `docs/_yardstick/`, or null. Validated as a reference only; the result file is #390's |
| `catalog.before`, `.after` | a 16-hex catalog version directory name, or null |
| `followups[].text` | text, never null |
| any other field | refused, by name |

`records.load_epics(directory)` reads every `*.yaml` of a directory and returns
the valid records and the errors by file name; an invalid file never stops the
others.
