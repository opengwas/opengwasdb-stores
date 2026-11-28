- **A Hybrid recipe can declare `post.rho` (issue #529).** `build-hybrid` and
  `complete-hybrid` plan `build-dense-rho` on the Store, after the build or completion, as
  Dense does, and `register` records `has_rho` for it. It needs `build-dense-rho` from opengwasdb 0.21.0 or later, which builds a Hybrid
  release's Rho Matrix in its Dense Component; the pin is 0.26.0. `post.rho` on a Ragged recipe is still refused, now naming the layout.
