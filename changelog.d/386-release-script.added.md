- **`tools/release.py` owns the bump, the notes and which merges are owed a
  version, so the release workflow holds no rule of its own (issue #386).** It
  turns the fragment types a merge added into the next version -- `breaking`,
  `added` or `changed` is a minor, `fixed`, `internal` or no fragment a patch,
  and from no tag at all the first feature release is `0.1.0` -- and into the
  notes, the fragments under their type headings plus a link to the pull
  request. `plan` reconciles: a lost run is not a lost version, because each run
  publishes every first-parent commit after the newest tag that has no tag yet,
  and every tagged commit whose Release is missing and is newer than the newest
  Release there is, oldest first; `publish` does only the steps still owed. Both
  ask the remote through `git ls-remote --tags`: a tag a failed push left behind
  is pushed on the retry, and a tag naming another commit is refused. Only newer
  missing Releases are repaired, so after the cutover the first run creates the
  cutover's Release and not one for every tag the project ever made. The notes of an older version are read from that
  commit's own tree, so a later edit to a fragment cannot rewrite a Release. It
  refuses a burst larger than 20 merges rather than replay a whole merged branch
  as versions, and it treats a git failure as an error rather than as "no tag
  yet", which would publish `0.0.1` over a real history.
