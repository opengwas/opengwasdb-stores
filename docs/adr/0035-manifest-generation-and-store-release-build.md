# Manifest Generation and Store Release Build meet at the Release Bundle

The repository runs two workflows. One turns a frozen Source Inventory into a
candidate bundle under `stores/<store-id>/`. The other turns an accepted bundle
into a built, validated, published Store Release. README, `workflow/README.md`
and ADR 0032 already describe the split, but the vocabulary for it grew up
by accident (epic #245, decided in #246 on 14 Oct 2026):

- **"Phase A" and "Phase B" record only the order the code was written in.**
  Phase A is the second workflow in data order. The two names appear about 200
  times, and `plan.py` already uses `phase` for build vs complete.
- **The meeting point has two names.** CONTEXT.md defined "Release Manifest"
  and "Release Manifest Candidate" for it. The code and docs say "Release
  Bundle" about 150 times, while CONTEXT.md never defined the term and listed
  "release bundle" under _Avoid_ for Release Artifacts.
- **Three verbs mean different things in the two workflows.** "preflight" is
  Phase B's inventory gate, Phase A's pre-write refusal stage, and CONTEXT's
  "Preflight Run", a subset build. "publish" and "finalise" each mean writing a
  candidate into `stores/` and also completing a Store's publication at the
  Artifact Root.

## Decision

**The two workflows are Manifest Generation and Store Release Build.**

- **Manifest Generation:** Source Inventory → candidate Release Bundle. It
  never builds, validates or publishes a Store.
- **Store Release Build:** accepted Release Bundle → built, validated,
  published Store Release, through `pixi run release`. It builds only a bundle
  whose Release Status allows a build (#247).

They share no DAG (`workflow/README.md`), and each depends only on the Release
Bundle contract, not on anything inside the other.

**The meeting point is the Release Bundle.**

- **Release Bundle** is the `stores/<store-id>/` directory: `release.yaml`,
  `build.yaml`, `analyses.tsv`, `validation.yaml` and sidecars. Candidate and
  accepted are Release Statuses of one thing, not two kinds of thing.
- **Release Manifest** narrows to the bundle's membership table,
  `analyses.tsv`.
- **Release Manifest Candidate** is retired and listed under _Avoid_. Say
  "candidate Release Bundle".

**Each overloaded verb has one meaning per workflow.**

| Word | Manifest Generation | Store Release Build |
|---|---|---|
| preflight | `pixi run preflight`, the inventory-readiness gate (unchanged) | the release entry point's pre-write checks (unchanged) |
| emit | write a candidate Release Bundle into `stores/` atomically, after the contract check | not used |
| publish, finalize | not used | publish a Store at the Artifact Root; finalize completes that publication transaction |

CONTEXT's "Preflight Run", a representative subset build with one human review
gate, becomes **Rehearsal Run**, so that no Preflight builds anything.

## Rejected alternatives

- **Other workflow names.** "Bundle Generation" names the first workflow after
  its output, and would have suited dropping "Release Manifest" altogether.
  "Candidate Generation / Release Build" is shorter, but "Release Build" reads
  as building any release asset. Neither is a bare "build phase", but the chosen
  pair keeps the existing term Manifest Generator.
- **One meeting-point word.** Folding everything into "Release Bundle" loses a
  name for the membership table that ADRs 0002 and 0003 and many docs use.
  Keeping "Release Manifest" for the whole directory means renaming about 150
  code and doc uses, `pixi run bundle-check` included.
- **Other splits for preflight.** Reserving the word for the inventory gate, or
  keeping it for the subset run, would each rename a working command or stage
  instead of a term with no implementation.
- **Qualify "publish" rather than split it** ("publish a candidate" vs
  "publish a Store"). This is the smallest change, but it leaves two meanings
  in use. "register" for Manifest Generation collides with Store Release
  Build's terminal `register` step.

## Consequences

- CONTEXT.md defines Manifest Generation, Store Release Build, Release Bundle,
  Emit, Publish, Preflight and Rehearsal Run, and narrows Release Manifest.
- **No code changes here.** The renames follow in their own tickets:
  - #248 builds the shared emitter under the name "emit";
  - #249 gives Manifest Generation its own package and pixi tasks;
  - #250 moves generation policy out of the contract;
  - #251 retires "Phase A / Phase B" from code, tests and docs.

  Until then, existing code and docs keep the old words.
- **ADR 0032 changes only in framing.** Its "Phase B candidate workflow" is
  Manifest Generation's first generator. Every decision in it stands: the
  resolver boundary, membership and acceptance policy, atomic staging and
  emit, and the human acceptance gate. "Phase A/B" stays in earlier ADRs as
  history; they are not rewritten.
- **Earlier ADRs say "Release Manifest" for the whole bundle** (ADRs 0002 and
  0003). Read those uses as "Release Bundle"; their decisions are unchanged.
