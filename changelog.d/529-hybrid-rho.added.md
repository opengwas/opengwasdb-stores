- **A Hybrid recipe can declare `post.rho` (issue #529).** `build-hybrid` and
  `complete-hybrid` plan `build-dense-rho` on the Store, after the build or completion, as
  Dense does, and `register` records `has_rho` for it. The engine pin moves to opengwasdb
  0.21.0 (`931ac166`), whose `build-dense-rho` builds a Hybrid release's Rho Matrix in its
  Dense Component. `post.rho` on a Ragged recipe is still refused, now naming the layout.
