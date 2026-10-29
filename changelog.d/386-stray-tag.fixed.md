- **A tag that is not `vX.Y.Z` can no longer break a build (issue #386).**
  `tag_regex` restricted what could be *parsed*, not what `git describe`
  *selected*, so a stray tag such as `99.0.0` was chosen and then failed to
  parse; the build died instead of using the newest release tag. The describe
  glob is now configured as well, and a test runs that exact command beside an
  unfiltered one so the restriction is shown to matter.
