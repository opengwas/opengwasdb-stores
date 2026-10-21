# Worktree prune (stores#312)

`tests/prune-worktrees/test_prune_worktrees.py` runs `tools/prune_worktrees.py`
against throwaway repositories under `$TMPDIR`: a bare origin with `main` and
`dev`, a primary checkout, and one worktree per state. Merged, squash-merged
(same patch, different commit) and a worktree whose name is only a prefix of a
path a process mentions are removable. Unmerged, dirty (tracked change,
untracked file), locked, and in use are kept, the last by a real `sleep` with
its cwd there, one with the path in its argv, and one running an executable
from it, each named by PID.

It also asserts that a dry run changes nothing; that `--apply` removes exactly
the removable worktrees, keeps what it kept untouched and deletes no branch;
that `--no-fetch` judges by the local `origin/*` refs and the default fetches;
and that only errors exit non-zero.
