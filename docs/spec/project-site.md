# Project site data

The specification of the project site's data (#389, epic #385): the **epic
record** an orchestrated epic commits when it finishes, and the **collectors**
`pixi run site-data` runs to gather everything else the site's pages read. The
pages themselves (#391) and the site build (#388) are specified elsewhere; this
file is the contract between the data and them.

The code is `src/project_site/` (ADR 0038). It imports `ogstores.bundle` and
`ogstores.paths` and nothing else from this repository, and it reads
`/data/opengwasdb/stores` only through read-only SQLite URIs.

## The served-path baseline

The build's served-path check reads one fixed list,
`resources/site/legacy-served-paths.txt` (#388, #397): the 90 paths under
`docs/` at `ff204b619ad8a5f671f4f67dd65d234096731102`, the commit the live
legacy Pages deployment was built from, one per line with `#` for comments. It
is fixed because it protects the links readers already hold: a page the site
generates may come and go without being added to it, and a path in it only goes
when a legacy URL is retired deliberately, by a reviewed change to the file. A
missing, empty or unreadable file fails the build rather than passing a render
nothing checked.

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
| `source` | `board`, `catalog`, `releases`, `stores`, `records`, `architecture` or `work` |
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
and `blockedBy` each return `pageInfo`). An issue's body is read for what the site
needs and dropped otherwise:

- a Milestone's checklist lines;
- an Epic's `purpose`, the first non-empty paragraph of the body that is prose
  (not a heading, a table, a quote, or a list of bullets and boxes), cut at a
  sentence (or, failing a sentence, at a word and an ellipsis) to at most 600
  characters;
- the paths and module names an Epic or sub-issue cites (`cited`), as written
  (see [Citations](#citations));
- a sub-issue's planned change to the architecture (`plan`; see
  [Planned changes](#planned-changes)).

A fenced block is skipped whole, tracked across the paragraph split: three or more
backticks or tildes open it and the same character in a run at least as long
closes it, so a code sample's own blank lines never end it and its second half is
never read as prose. A relationship that still has a page unread when the
response is parsed is an
error, so the file is `available: false` rather than a short list marked
complete.

| Key | Content |
|---|---|
| `epics` | one row per Epic: `key` (`repo#n`), `title`, `url`, `state` (the issue's, `OPEN` or `CLOSED`), `status`, `workstream`, `next`, `priority`, `sub_issues` (`{total, completed}`), `purpose`, and `children` |
| `epics[].purpose` | the epic's own first prose paragraph, cut as above, or `null` when its body has none |
| `epics[].children` | the epic's sub-issues: `key`, `title`, `url`, `state`, `cited`, and `blocked_by`, a list of `{key, state}` |
| `epics[].children[].plan` | the ticket's planned change to the architecture (#422), present only when its body has an `architecture` block: the parsed plan, or `{invalid: reason}`; see [Planned changes](#planned-changes). The body itself is not kept |
| `epics[].cited`, `epics[].children[].cited` | the code the issue's text cites (#419): a sorted list of paths (`opengwasdb/encoding/se_range.py`, or the path of a GitHub link to a file) and dotted module names (`opengwasdb.encoding.se_range`), as written and not checked against any tree; `[]` when it cites none. The Work map resolves them through the claims. The text itself is not kept |
| `milestones` | one row per Milestone, with the same fields as an epic (no `children`, no `cited`) and `lines` |
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

### Citations

One extractor and one resolver serve the tickets (Work) and the ADRs
(Architecture): `project_site.collect.architecture_adrs.cited_strings` finds what a
text cites and `project_site.collect.citations` says which tracked file a path means.

- **What is cited.** A path with a directory and a code extension (`.py`, `.smk`,
  `.sh`, `.toml`), the path of a GitHub link to a file, or a dotted module name. A
  trailing `:line` (`:551`, `:10-20`) is not part of the path.
- **Which file a path means.** The file itself when it is tracked (in the issue's own
  repository first); otherwise the one tracked file of either repository that ends
  with `/<path>`, so `encoding/codec.py` means `opengwasdb/encoding/codec.py`. When
  **more than one** file ends that way the citation is **unresolved**: it places
  nothing, is never guessed, and is listed with its candidates (`work.json`
  `unresolved`, a count on the Work page).
- **Dotted names.** A module name means `a/b/c.py` (`src/a/b/c.py` in this
  repository) or its package's `__init__.py`, shortened a name at a time.

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
| `versions[].selection` | the `selection` table's rows in `store_id` order -- `store_id`, `decision` (`listed`, `excluded`, `refused` or `placeholder`) and `reason` -- for the version `current` names; `null` for every other version, whose decisions the site does not show |

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
result schema and validator are #390's (`docs/spec/yardstick.md`): the collector
reads each result through `yardstick.result.validate`, and reports a refused one
under `yardstick.invalid` with its `ResultError` message. Any *other* exception
from validating one file -- a validator that raises rather than refuses -- is
that file's error too, recorded as `ExceptionType: message`, and the other
results are still read: one bad file never makes the source unavailable. The
same holds for the epic records above. `records.collect`
takes another validator as an argument, for tests.

### Architecture (`architecture.json`)

The architecture of both repositories (#418, epic #416), from the two
hand-written model files `docs/architecture/components.yaml` and the code they
claim. It reads **git, not checkouts**: this repository at `HEAD` (tracked files
only) and the engine at its `main` through the bare clone the site build already
keeps (`/data/opengwasdb/work/site/opengwasdb.git`, or `--engine-git`; `--engine-ref`
names another ref). `pixi run site` fetches `main` into that clone first (`--refresh-engine`);
a failed fetch leaves the `main` the clone has and is recorded in `engine.refresh`. The site's *docs* mirror stays at the
engine revision `pixi.lock` pins; the map shows current work, so it does not.

The model files. Each has `repo` and `components`: `id`, `name`, `container`,
`stages`, `summary`, `paths`. The engine's has `layers` (top to bottom, `layer`
and `name`) and a `layer` on every component. This repository's has `system`,
`people`, `external`, `containers`, `container_relations` and `stages`. A path
ending in `/` claims a directory; any other path claims one file; the longest
claimed prefix wins. Nothing else is written by hand: edges, counts and ADR
placement are measured.

| Key | Content |
|---|---|
| `registry`, `engine` | `repo`, `ref`, `commit` read, `committed` (that commit's date), `refresh`, `model` (`true`, or `false` when the ref has no model file) and `note`. The engine's `refresh` is `null` when no fetch was asked for, else `{ok, error}` with git's own message; the pages show the commit and its date, and a visible line when the refresh failed |
| `model` | `system`, `people`, `external`, `containers`, `container_relations`, `stages` (this repository's), `layers` (the engine's; `[]` without its model) |
| `components` | this repository's, then the engine's: the model's keys plus `repo` (`registry` or `engine`), `files` and `lines` (claimed code files), `tracked` (every tracked file claimed) |
| `files` | `{registry: [...], engine: [...]}` (#422): every tracked code file of each tree at the ref read (the files the ownership rule counts as code), sorted, read NUL-safely. A planned `adds` is told to exist from it. Added to the file; no other field changed |
| `gaps.<repo>` | `unclaimed` (code files no component claims, by path) and `stale` (`{path, component}` claims matching no tracked file) |
| `baseline` | `path`, `entries` (how many imports the committed baseline lists; `null` with a `note` when it is not in this repository at the commit read, never 0) and `measured` (the engine imports the code makes) |
| `edges` | `from`, `to`, `imports`, `scope` (`engine`, `registry`, `registry_to_engine`), `upward` |
| `upward_imports` | each engine import that goes up a layer (#419), sorted by `file` then `name`: `file` (the importing file), `name` (the absolute imported name, a relative import resolved), `from`, `to` (component ids), `from_layer`, `to_layer`. The engine's layering baseline lists the same pairs. `null` when the engine has no model at the ref (the layering cannot be measured), like `summary.upward_imports`; never `[]` |
| `adrs` | every `docs/adr/NNNN-*.md` of both repositories: `repo`, `number`, `path`, `title`, `status`, `url`, `components` |
| `summary` | `edges`, `upward_edges`, `upward_imports`, `registry_to_engine_edges`, `registry_to_engine_imports`, `adrs_total`, `adrs_placed`; the engine's half of the graph is `null` without its model (see below) |

A code file that cannot be read, or a Python file that does not parse, makes the
source unavailable with an error naming the path; a count is never a guess of 0.
A claimed script under `tools/` or `workflow/` is a source of imports even though
no one can import it.

Code is a tracked `.py` under `opengwasdb/` in the engine, and a `.py`, `.smk`,
`.sh`, `.R`, `.r` or `Snakefile` under `src/`, `workflow/` or `tools/` here (R counts for
ownership and lines, not for the Python import graph). **Gaps never
fail the build.** The engine's own CI fails on its unclaimed files, and this
repository's test (`tests/project-site-architecture/test_architecture_ownership.py`)
fails on its own; the collector only reports them, on the page. An engine ref with
no model file is not an error either: `engine.model` is `false`, `engine.note`
names the ref and commit, the engine has no components, edges or gaps, and this
repository's half is complete. Everything on the engine's half that its claims
resolve is then `null` in `summary`, never 0: `edges`, `upward_edges`,
`upward_imports` and `registry_to_engine_edges` (the seam's component pairs).
`registry_to_engine_imports` stands, because it is measured from the names, and
the page says the layering cannot be measured until the engine's model is at the
ref read. A ref that cannot be read at all makes the source
unavailable.

Files are read by object id, so a path holding a newline is its own file. An
**import** is resolved against every tracked module first, then to that module's
owner: an unclaimed module gives no edge (it is a gap), and never takes its
package's owner. An import is counted per (file, imported name) between modules that components
claim, relative imports resolved. An edge is `upward` when it is inside the engine
and goes to a component on a higher layer; the same layer and lower layers are not.
`registry_to_engine` is the seam between the repositories.

An **engine import** is a name that is `opengwasdb` or starts with
`opengwasdb.`, counted per (importing file, imported name) over this
repository's claimed Python files. The name alone decides it, so the seam does
not need an engine checkout or the engine's model: `registry_to_engine_imports`
is that count, and it stands whether or not the engine's model is there.
`registry_to_engine_edges` is the number of component pairs those imports make,
which the engine's claims resolve, so it is `null` when the engine has no model
at the ref read — unknown, never 0. `baseline` reports
`docs/architecture/engine-imports-baseline.yaml`, which lists those imports and
only shrinks: `tests/project-site-architecture/test_engine_imports_baseline.py`
fails on an import the file does not list (naming the file, the name and, when
the engine was read, the engine component it lands in) and on a listed import
that is gone. One measurement feeds both the seam counts and the baseline, so
they cannot disagree.

An **ADR** is placed by the code it cites (a path, a dotted module name such as
`opengwasdb.store.paths`, or a GitHub link to a file, resolved through the
claims; a bare suffix such as `store/paths.py` only when exactly one tracked file
ends with it) and by a component's `name` in its title (case-insensitive, at the start of a word, with no boundary at its end). A
component's name that appears only in the body places nothing, however often.
`components` maps a component id to `code` or `title`; an ADR placed nowhere has
`{}` and is listed on the page.

### Work (`work.json`)

Where live work meets (#419, epic #416). It reads the `board.json` and
`architecture.json` the same run just wrote to the data directory, so it runs
after them and is **unavailable, naming which, when either is**
(`work is unavailable: board is unavailable: ...`). From those two files and the
branches it places every piece of live work on the components the claims name.
Agents read this file, not the page; `schema_version` is `1` and changes when a
key is removed or its meaning changes. Keys are only ever added within a version.

**Owners.** An owner is an open epic whose board `status` is Active, Todo or
Paused (`kind: epic`); otherwise the epic or ticket a branch's name points at
that the board does not list (`kind: issue`, status `Off board`, or the status
of a closed epic); otherwise the branch itself (`kind: branch`, labelled by its
name). A branch's owner is the epic its name gives (`orch/<epic>/<ticket>`, with an
optional `-slug` after the ticket; `orch/<epic>-...`), looked up in either repository, else its ticket's epic
(`fix/issue-345`, `345-...`), else the ticket, else the branch.

**Branches.** A branch counts when it is ahead of `main` and its newest commit
is within `days` (21) days. Its files are `git diff --name-only -z main...<branch>`.
The published page reads **pushed** branches only: the engine's bare clone after
`git fetch origin +refs/heads/*:refs/heads/*`, this repository's checkout as
`refs/remotes/origin/*` after `git fetch origin`. A failed fetch is recorded in
`branches.sources[].fetch` and shown on the page, and the refs already there are
read. The `conflicts` command can add the local branches of other git
directories (`--local`), which the page never shows. A branch read from two clones
counts once, as the copy further ahead (a tie keeps the pushed one), and
`branches.sources[].count` is what each read found.

**Signals.** One row per (component, owner, item, via, state), with `what`, the
branches (`repo:name`) or cited strings behind it. `via` is `branch`, `cited`
(an open ticket) or `scope` (an epic's body). The `state` is:

| State | When |
|---|---|
| `building` | a branch's diff touches a file the component claims |
| `active` | an open ticket of an Active epic cites it, and the ticket is not blocked |
| `waiting` | an open ticket cites it and waits for an open ticket (a blocking edge whose blocker is still open) |
| `scope` | an epic's body cites it |
| `planned`, `paused` | an open ticket of a Todo or Paused epic cites it, not blocked |
| `stale` | a branch whose epic is closed or not live, or whose ticket is closed |

A cited string is placed through the claims, in the repository its text is in first,
after it is resolved to a tracked file as [Citations](#citations) say (from
`architecture.json`'s `files`). A path that is no tracked code file is judged as
written; a citation that is ambiguous is `unresolved`, and one no claim covers places
nothing. Closed tickets add nothing.

**Meets.** A component is a meet when two or more owners have `building` or
`active` signals on it. For every pair of its owners there is a `pairs` entry,
with a `coordination`:

- `coordinated`: a blocking edge `{waiting, on}` links a specific item of one with a
  specific item of the other, in either direction. A shared epic, or an edge between
  other tickets of the same epics, does not coordinate it.
- `unlinkable`: no edge links them, and one side's work is an **epic's integration
  branch** (`orch/<epic>-slug`), whose item is the branch (`branch:<repo>:<name>`; decided by the branch's own name, so it holds whether or not the board lists the epic):
  it belongs to the epic as a whole and has no ticket an edge could link, so the
  absence of an edge proves nothing. `epic_branches` names those branches.
- `uncoordinated`: no edge, and every item is a ticket (or a branch on no epic).

`coordinated` on a pair stays `true` only for `coordinated`. The meet's `coordination`
is `uncoordinated` if any pair is, else `unlinkable` if any pair is, else `coordinated`;
its `coordinated` is `true` only for the last. The page and `conflicts` never call an
`unlinkable` meet uncoordinated.

**Unmeasured.** Without the engine's architecture model at the ref read
(`architecture.engine.model` false) no engine file belongs to a component, so
engine-side work is **unknown**, never "no work": `unmeasured` is
`{"engine": <reason>}` (empty otherwise), engine signals and meets are not
produced, the page says engine-side work was not measured, and `conflicts` on
engine files or an engine branch exits 2. Registry-side meets are still measured.

| Key | Content |
|---|---|
| `schema_version` | `1` |
| `inputs` | `board.collected_at`; `architecture.collected_at` and the `ref`/`commit` read of each repository |
| `owners` | the owners with at least one signal: `key`, `kind`, `title`, `url`, `status`, `workstream` |
| `components` | the components with at least one signal: `id`, `name`, `repo` |
| `signals` | as above: `component`, `owner`, `item`, `via`, `state`, `what` |
| `unmeasured` | `{}`, or `{engine: reason}` when the engine's model is missing (see above) |
| `unresolved` | the citations of live tickets and epics that more than one tracked file ends with: `owner`, `item`, `cited`, `candidates` (the paths) |
| `meets` | `component`, `owners`, `items` (owner to its items there), `pairs` (`owners`, `coordinated`, `coordination`, `edges`, `epic_branches`), `coordinated`, `coordination` |
| `plans` | the tickets' planned changes and, for a closed ticket, what was checked, as above (#422) |
| `waits` | every open blocking edge among the live epics' tickets: `{item, on}` |
| `tickets` | every open ticket of a live epic, to its owner (what `conflicts --ticket` reads) |
| `branches.sources` | per repository read: `repo`, `git_dir`, `main`, `days`, `fetch` (`null`, or `{ok, error}`), `local`, `count` |
| `branches.list` | `repo`, `name`, `ahead`, `committed`, `files`, `epic`, `ticket` (from the name), `local`, and the resolved `owner`, `item`, `components` |
| `summary` | `owners`, `meets`, `coordinated`, `uncoordinated`, `unlinkable` (these three add up to `meets`), `branches`, `unresolved` (the length of `unresolved`) |

The page's tiles are `summary`; every other figure on it is a count of these rows.

**The command line.** `pixi run architecture` (`python3 -m project_site.architecture`)
runs the same collectors on the spot, or reads a collection with `--data DIR`:

- `check`: for each repository, the code files no component claims and the
  claims that match no tracked file, and the engine's `docs/architecture/layering-baseline.yaml`
  at the ref read, entry by entry (importing file and imported name, never a count, so a new import
  cannot hide behind a fixed one): each `new:` import not in the baseline and each `stale:` entry no longer
  imported is named; no baseline file is not a failure. What cannot be measured is
  printed with its reason and is **unknown, never a pass**: an engine ref with no model, a repository or file
  that cannot be read, a file that is not YAML, has no `allowed` list or has an entry that is not a `file`
  and a `name` of text. Exit **1** if anything measured has a problem (an unclaimed or stale claim, a new or
  stale baseline entry), whatever else is unknown; otherwise **2** if any part could not be measured;
  otherwise 0.
- `conflicts --branch B [--repo engine|registry]` or `--files PATH... [--ticket repo#n]`:
  the components the work touches, the other owners' `building` or `active` work on each
  with its ticket, epic, branch and cited files, and whether a blocking edge links `--ticket`
  (or the branch's ticket) to theirs. Exit 1 when any meet is uncoordinated or unlinkable (asking a person is the safe default; the message says which); 0 when none is;
  **2 when it cannot decide** (a source is unavailable, the branch is not in the
  collection, a git directory or branch diff cannot be read, or the work is the engine's and its model is missing). `--local GIT_DIR...` adds those clones' local branches, `--refresh` fetches first.

### Planned changes

A ticket that changes the structure says so in its body (#422, epic #416), in one
fenced block whose info string is `architecture`, holding YAML. Text outside the
fence is ignored, and a ticket without a block has no plan.

````markdown
```architecture
adds:      # paths that will exist, and the component that will claim them
  - paths: [opengwasdb/encoding/association.py]
    component: encoding          # an existing component id, or a new id ...
    name: Association encoder    # ... which then needs a name (and `container`, `layer` if engine)
moves:     # paths whose claim changes component
  - paths: [opengwasdb/build/eaf_orientation.py]
    to: resolution
drops:     # component imports that should disappear, "from -> to"
  - dense -> encoding
absorbs:   # free text, drawn on the map, never checked (line numbers drift)
  - encoding/codec.py:932
```
````

Every key is optional, but a block with none of them is invalid, and so is any
other key (`{}` is a block with none). Each key present must hold a list; `paths`
are non-empty lists of text; `name` and `container` are text and `layer` a whole
number. A component id (`component`, `to`, either end of a drop) is lower-case
letters, digits and `_`, starting with a letter (the model's form, so it can never
be markup), and must be an existing component or declared in `adds`; a new one needs a
`name`, and a new engine component (paths under `opengwasdb/`) a `container` and a
`layer` too. The board
collector parses the block into `children[].plan` with `adds`, `moves`, `drops`
(each `{from, to}`) and `absorbs`, every key present as a list. A block that
cannot be used is never dropped: `plan` is `{invalid: reason}` (not YAML, unknown
key, empty, a malformed item, more than one block, a block never closed) and the
page shows the reason. The board collector does not know the components, so the
`work` collector judges their ids once it knows them: when the engine's model
is missing it cannot, and an id it does not find is not called unknown.

**Judging a plan.** The `work` collector lists every ticket that has a plan, of
any epic, open or closed, as `work.json`'s `plans` (sorted by epic, ticket):
`ticket`, `epic`, `title`, `url`, `state` (`open` or `closed`), `plan`,
`components` (each component the plan involves: `id`, `name`, `repo`, and `new`,
true for one the plan declares) and `checks`. An open ticket's plan is carried
as planned and `checks` is empty, as it is for an invalid plan; `absorbs` is
carried and never checked. Once the ticket is closed each item is checked
against what the architecture collector read (the engine at its ref, this
repository at the site's checkout):

- `adds`: for each path, `{kind: adds, path, component, held, why}`; held only when
  the path is a tracked file (in `architecture.files`) **and** the named component
  claims it. A path that does not exist is not held, saying so with the ref; without
the engine's model that is still a measured fact (the tree was read), and the
`why` says the claims were not judged.
- `moves`: the same, with `to` as the component.
- `drops`: `{kind: drops, from, to, imports, held, why}`; held when no import goes
  from `from` into `to`, else not held, naming how many remain. Without `architecture.edges`
  (absent or `null`) it is unknown: absence is never zero imports.

Every check also has `repo` (`registry` or `engine`), the tree it was read in;
the page's warning names that repository's ref and commit.

`held` is `true`, `false`, or `null` for **unknown**: without the engine's model
(`architecture.engine.model` false) an engine path that no registry claim covers,
a path whose existence is not known (no `architecture.files`, the tree not read,
or not a code file, since only code files are listed), and a drop with an end that is not a registry component, cannot be judged, and
`why` says the model is missing at that ref. Missing the model is never an unmet
item. An unmet item is a warning on the page, never a build failure: the ticket
may have closed on an epic's integration branch whose PR has not merged to the
ref read. The `plans` key is additive, so `schema_version` stays `1`.

**The page.** The Structure and health page ends with a `## Planned changes`
section, written from `plans` alone. Each valid plan has a heading (the ticket,
its title and state), a Quarto `{dot}` diagram of the components it involves
(added paths marked `+`, moved paths `→`, a declared component dashed and
labelled `new`, a dropped import a dashed arrow), and its items: an open
ticket's as planned, a closed one's as `held`, `not held` or `unknown` with
`why`. `absorbs` is listed as text. A closed plan with unmet items carries a
warning, naming the ref read and that the ticket may have closed on an
integration branch not yet merged. An invalid plan shows its reason and no
diagram. A `work.json` without `plans` is said to predate planned changes, never
shown as "no plans"; with `plans` empty the page says no ticket carries a block.

### The command's output and exit status

One line per source, in this order (the architecture line is longer: components,
gaps, upward imports, the seam and ADRs placed; `work` runs last, because it reads the
board and architecture files just written, and adds `BRANCH FETCH FAILED` for a failed fetch), then the files are all written:

```text
board: 17 epics, 2 milestones, 16 milestone lines
catalog: 3 versions, current 1b3a155d449bb84a
releases: 0 releases, 1 tags
stores: 22 stores
records: 0 epics, 0 invalid, 0 yardstick results, 0 invalid
architecture: 10 registry + 15 engine components (engine model at main 1a2b3c4d), gaps ...
work: 3 owners, 2 meets (1 coordinated, 1 uncoordinated), 2 branches
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
