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
rendering it. An epic record exists only for an issue the board counts as an
Epic (a ticket is not one). The orchestrator writes it from the run's
`state.json`, and an agent writes it in one pass, because every field is a
scalar, a list of flat rows, or a mapping of two keys. `status` and `finished`
follow the **epic issue**, not the run: open means `active` with `finished:
null`, closed means `finished` with the date the issue closed. A run finishing
does not finish its epic.

**An unknown value is `null`, never an estimate.** Every field is required, so
that a field left out is distinguishable from a field not known. A typed value
that is not the real thing is refused: a date that is not an ISO date, a number
that is not a number, a commit, `repo#n` key, `docs/_yardstick/` path or catalog
version that does not have its form. **Prose is free text**: `title`, `summary`,
decision text and follow-up text only have to be non-empty, so history that
says "Estimated N for eQTLGen" is not mistaken for a guess.

```yaml
repo: opengwasdb-stores            # the epic's repository, without the owner
number: 313                        # the epic's issue number
title: Analysis catalog across all Stores
status: active                     # active while the epic issue is open, finished once closed
workstream: Analyses               # a board Workstream, or null
started: 2026-10-21                # ISO date, or null
finished: null                     # ISO date the issue closed; null exactly while active
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
test_time:                         # always a mapping; each member null when never measured
  baseline: {commit: e6e829d, wall_s: 158.4, suites: null}
  final: {commit: a84ecfb, wall_s: 162.05, suites: null}
yardstick:                         # always a mapping; each member a docs/_yardstick/*.json path (#390) or null
  before: null
  after: null
catalog:                           # always a mapping; each member a 16-hex catalog version, or null
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
| `title` | non-empty text, never null |
| `status` | `active` or `finished`: open or closed, as the epic issue is |
| `workstream` | one of Data architecture, Build architecture, Interface, Store builds, Tool integration, Analyses, Engineering, or null |
| `started`, `finished` | an ISO date (`2026-10-21`, unquoted or quoted) or null. A finished epic has a `finished` date, the day the issue closed; an active one has none |
| `summary` | non-empty text of 2 to 4 sentences, or null |
| `prs`, `tickets[].key`, `tickets[].pr`, `followups[].key` | `repo#n`: a repository name, `#`, an integer of 1 or more |
| `versions` | tag names. Empty until each repository merges to `main` and tags every merge (#386) |
| `tickets[].merge` | a hex commit of 7 to 40 characters, or null. A ticket with no recorded merge commit is null, never its PR's head |
| `decisions[].date`, `.text` | an ISO date and non-empty text, both required; `link` is an `http(s)` URL or null |
| `test_time` | always a mapping with `baseline` and `final`; each is null, or `{commit, wall_s, suites}`: a hex commit or null, seconds as a number or null, and `suites` null or a mapping of suite name to seconds. A run state records one total, so `suites` is null for every backfilled record |
| `yardstick` | always a mapping with `before` and `after`; each is null, or a relative path that starts `docs/_yardstick/`, ends `.json` and has no `..`. Validated as a reference only; the result file is #390's |
| `catalog` | always a mapping with `before` and `after`; each is null, or a 16-hex catalog version directory name |
| `followups[].text` | non-empty text, never null |
| any other field | refused, by name |

`records.load_epics(directory)` reads every `*.yaml` of a directory and returns
the valid records and the errors by file name; an invalid file never stops the
others.

## The collectors

```text
pixi run site-data [--data-dir PATH] [--catalog-root PATH] [--registry PATH] [--strict]
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
aliased query, and follows every further page of both relationships (`subIssues`
and `blockedBy` each return `pageInfo`). The issue bodies of everything but
Milestones are dropped. A relationship that still has a page unread when the
response is parsed is an error, so the file is `available: false` rather than a
short list marked complete.

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

### Records (`records.json`)

| Key | Content |
|---|---|
| `epics` | the valid epic records of `docs/_epics/*.yaml`, dates as ISO text |
| `invalid` | each invalid file's name, and its errors (`validate_epic`'s, or `could not be read: ...`) |
| `yardstick.results` | each valid `docs/_yardstick/*.json` as `{name, result}` |
| `yardstick.invalid` | each invalid file's name and errors |

An invalid file is reported by name and never stops the others. An absent or
empty `docs/_epics/` or `docs/_yardstick/` is not an error. The yardstick's
result schema and validator are #390's (`docs/spec/yardstick.md`): until it
lands the collector checks only that a result is a JSON object, and the
validator is passed to `records.collect` when it does.

### The command's output and exit status

One line per source, in this order, then the files are all written:

```text
board: 17 epics, 2 milestones, 16 milestone lines
catalog: 3 versions, current 1b3a155d449bb84a
releases: 0 releases, 1 tags
stores: 22 stores
records: 0 epics, 0 invalid, 0 yardstick results, 0 invalid
```

An unavailable source prints `UNAVAILABLE <source>: <error>` in place of its
line. The exit status is **0** all the same: every file is written, and the
pages show the notice from the file's `available` and `error`. `--strict` exits
1 when any source is unavailable (after every file is written), for a caller
that must not go on without it.

## Backfill

The records under `docs/_epics/` are written from the orchestrate run states
on the host (`~/.cache/orchestrate/*/state.json`,
`/data/opengwasdb/work/epic*/orchestrate/*/state.json`), read-only. A run state
holds no start or finish date, no PR merge date and no per-suite test time, so
from a state:

- `status` and `finished` come from the epic **issue**, not the state: while the
  issue is open the record is `active` with `finished: null`, however many of
  its PRs the run has merged, and once it is closed the record is `finished`
  with the date it closed;
- `started` is the earliest `decisions[].when`;
- `tickets` are the state's ticket entries, with `merge` the merge commit where
  one is recorded and `null` where only a PR is named;
- `decisions` are the state's `decisions[]`, each with its date and no link;
- `test_time` is the state's `baseline` and `final`, with `suites: null`;
- `yardstick` is `{before: null, after: null}` and `versions` is `[]`: the
  yardstick (#390) and the versions (#386) do not exist yet;
- a ticket's `key` is its issue number under the run's repository, or the
  sibling repository's when the entry is named for it (`opengwasdb#298`), and
  `null` for work with no issue of its own (a split part, an operation, a
  review pass). Its `pr` is the PR the entry names, and its `merge` is the
  commit it records, `null` where the entry records none or only a pin;
- a run in stages (#282) has an archive per stage beside its `state.json`
  (`state-stage1.json`, `state-stage2.json`): `tickets`, `decisions` and `prs`
  cover all of them, a ticket a later stage finished replaces its earlier entry,
  and `test_time` is the last stage's, whose baseline and final are measured on
  the same test runner;
- `catalog` is `{before: null, after: null}` where the run state does not name
  the versions.

An epic record exists only for an issue the board counts as an Epic. A run on a
ticket (#241, a follow-up run on a ticket branch) is not an epic and has no
record. The backfilled records are #313, #282 and #302, all still open and so
`active`; #313 also waits on OGS-00011, which is not validated. Runs still in
flight (#232, #371, #385) and #312's partial run were not backfilled.
