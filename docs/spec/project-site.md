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

## The collectors

```text
pixi run site-data [--data-dir PATH] [--catalog-root PATH] [--registry PATH]
```

`pixi run site-data` runs every collector and prints one line per source. Each
collector writes `<data dir>/<source>.json`. The data directory is
`docs/_data/` of the checkout, which git ignores, unless
`OPENGWASDB_SITE_DATA` or `--data-dir` names another. Nothing is ever written
under `/data/opengwasdb/stores`.

Every file starts with the same four keys, then the source's own:

| Key | Meaning |
|---|---|
| `source` | `board`, `catalog`, `releases`, `stores` or `records` |
| `available` | `true` when the source was read; `false` when it could not be |
| `collected_at` | UTC time, `2026-10-29T12:00:00Z` |
| `error` | `null`, or the failure as `ExceptionType: message` |

A source that fails (`gh` offline, a catalog root that does not exist) writes
`available: false` with its error and no other key, and never stops the other
collectors. An **empty** source is not a failure: no Releases yet is
`available: true` with empty lists. A page reads `available` before it reads
anything else.

### Board (`board.json`)

The board collector has its own GraphQL query (`project_site.collect.board`);
this repository does not import the board skill. It reads project 1 of the
`opengwas` organisation, every page of it, and then every epic's sub-issues and
their blocked-by edges (`subIssues` and `blockedBy` in the GraphQL API) in one
aliased query. The issue bodies of everything but Milestones are dropped.

| Key | Content |
|---|---|
| `epics` | one row per Epic: `key` (`repo#n`), `title`, `url`, `state` (the issue's, `OPEN` or `CLOSED`), `status`, `workstream`, `next`, `priority`, `sub_issues` (`{total, completed}`), and `children` |
| `epics[].children` | the epic's sub-issues: `key`, `title`, `url`, `state`, and `blocked_by`, a list of `{key, state}` |
| `milestones` | one row per Milestone, with the same fields as an epic (no `children`) and `lines` |
| `milestones[].lines` | each `- [ ]` or `- [x]` line of the body, in order: `checked`, `text`, and `refs` (`repo#n` keys) |
| `items` | every other project item, with the epic's fields |

- `status`, `workstream`, `next` and `priority` are the board's field values as
  written, and `null` where the item has none.
- An Epic is an issue of type Epic, labelled `epic` or `complexity:epic`, or
  titled `Epic:`; a Milestone is of type Milestone or titled `Milestone:`. A
  Milestone is never an Epic. The rule is the board skill's.
- A checklist is read from lines that begin `- [ ]`, `- [x]` or `- [X]`, after
  any indentation, and from nothing else: a bullet without a box is prose. A
  bare `#n` in a line is an issue of the milestone's own repository; a
  reference into another owner is dropped.
- `tests/project-site-data/fixtures/board-graphql.json` is a response recorded
  once from the real API by `board.fetch`.

### Catalog history (`catalog.json`)

Every version directory under `<artifact root>/catalog/versions/`
(`/data/opengwasdb/stores/catalog/versions/` here); `--catalog-root` names
another root. Each `catalog.sqlite` is opened through a `file:...?mode=ro` URI.
A root that does not exist is an error; a root with no versions is `versions: []`.

| Key | Content |
|---|---|
| `root`, `current` | the root read, and the name of the version `current` points at, or `null` |
| `versions` | oldest `built_at` first, one row per version |
| `versions[].version`, `.current` | the directory name (16 hex), and whether `current` names it |
| `versions[].catalog_meta` | the `catalog_meta` table as a mapping: `built_at`, `registry_commit`, `builder_version`, `serving_policy` (JSON text), as written |
| `versions[].counts` | the `counts` table's rows as they are, in table order: `dimension`, `value`, `n_analyses` |
| `versions[].totals` | `analyses`: `count(*)` of `listed_analyses`; `stores`: the Stores those rows belong to |
| `versions[].changes` | `changes.tsv`'s rows tallied by change (`{added: n, ...}`), or `null` for a version without the file |

`counts` rows are per dimension and **overlap**: an Analysis is counted once in
each dimension, and `NULL` is a category, so a dimension's rows sum to the
total only when none is missing. `totals.analyses` comes from SQL on the
listed Analyses, never from summing dimension rows.

### Releases (`releases.json`)

Both repositories' tags and GitHub Releases, from `gh api repos/opengwas/<repo>/tags`
and `.../releases`.

| Key | Content |
|---|---|
| `repos` | `opengwasdb`, then `opengwasdb-stores` |
| `repos[].tags` | `name` and the tagged `commit` |
| `repos[].releases` | `tag`, `name`, `published_at`, `draft`, `prerelease`, `url` |

A repository with no Release has `releases: []` and the file is still
`available: true`. A tag is not a Release: until #386 each repository's
Releases are empty, and `opengwasdb` has one tag, `v0.2.0`.

### Stores (`stores.json`)

Every registered Store Release of the registry of the checkout being rendered
(`stores/`; `--registry` names another), never `/data`. A registered Store is
a directory named `OGS-xxxxx`; `by-label/`, `README.md` and `.staging/` are
not. Each bundle is read with `ogstores.bundle.load`, which never opens a Store.

| Key | Content |
|---|---|
| `registry`, `stores` | the registry read; one row per Store, in id order |
| `stores[]` | `store_id`, `label`, `status`, `trial`, `layout`, `completion_state`, `association_coverage`, `access_posture`, `derived_from`, `superseded_by` |
| lifecycle | `created_at`, `accepted_at` (release.yaml), `validated_at`, `validation_status` (validation.yaml) |
| `observed` | validation.yaml's `observed` mapping as written (`n_analyses`, `n_variants`, `n_associations`, `store_bytes`, ...), or `null` without a Validation Record |
| `description`, `notes` | release.yaml's text as written. `notes` is where Release Errata live, and the catalog shows it |
| `problems` | what the bundle could not state: a `trial` that is not a boolean, a coverage outside the vocabulary, a malformed YAML file. Empty when there is nothing |

A fact the bundle does not hold is `null`: a Store with no Validation Record
has `validated_at: null`, never a date borrowed from elsewhere, and a
`n_associations` the record left `null` stays `null` (it is not
`n_variants × n_analyses`). This collector does not run `bundle.check()`;
`pixi run bundle-check` does.
