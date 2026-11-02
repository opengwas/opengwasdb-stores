- **The site can now show where live work meets, and agents can ask the same
  question from a terminal (issue #419, epic #416).** A `work` collector
  places every branch diff and every file an open ticket cites on the component
  that claims it, and reports where two owners' work meets and whether a
  blocking edge between the specific tickets coordinates it. The Work map page
  is drawn from its documented, versioned `work.json`, says when it was measured
  and that branches existing only on a build host are not shown; `pixi run
  architecture check` and `conflicts` answer for agents and exit non-zero on a
  claims gap or an uncoordinated meet. The board collector now also keeps the
  paths and modules each epic and ticket text cites (`cited`), and no body.
