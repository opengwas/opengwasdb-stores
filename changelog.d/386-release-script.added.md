- **`tools/release.py` owns the bump and the release notes, so the release
  workflow holds no rule of its own (issue #386).** It turns the fragment types
  a merge added into the next version -- `breaking`, `added` or `changed` is a
  minor, `fixed`, `internal` or no fragment a patch, and from no tag at all the
  first feature is `0.1.0` -- and into the notes, the fragments under their
  type headings plus a link to the pull request. A push with no pull request
  names its commit subject instead, and a pull request with no fragments says
  so, because an empty release whose notes do not explain why is the silent
  failure this project treats as the worst outcome. A fragment the release
  cannot read, a tag that is not `vX.Y.Z`, and an unknown type are refused with
  a message rather than guessed at.
