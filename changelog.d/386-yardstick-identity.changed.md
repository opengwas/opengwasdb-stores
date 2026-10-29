- **A yardstick result records the `ogstores` commit it ran, not only the
  version (issue #386).** An editable install reports the version it was built
  from, which is not necessarily the code that ran, so `ogstores.commit` (the
  checkout's 40-hex `HEAD`) and `ogstores.dirty` join `ogstores.version` in the
  result, in `src/yardstick/result.py`'s contract and in
  `docs/spec/yardstick.md`. `schema` stays `1`: no result is committed yet, so
  nothing a reader knows is invalidated.
