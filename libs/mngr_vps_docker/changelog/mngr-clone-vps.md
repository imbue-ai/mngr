Fixed `mngr create` against the VPS Docker backends (aws/vultr/ovh) failing during the
post-build git seed with `remote rejected ... refusing to update checked out branch` when
the build context is a primary git checkout (`.git` is a directory) that has linked
worktrees -- e.g. running `mngr create -t aws` from a main checkout that keeps a worktree
per branch.

The remote-`docker build` flow now clones *any* local git context into a temp dir before
upload (previously only a linked worktree, whose `.git` is a gitlink file, or an explicit
`--git-depth`, triggered the clone). A fresh clone's `.git` is self-contained and carries
no `.git/worktrees/` admin, so it no longer baked the operator's other branches into the
image as "checked out" -- which is what made the mirror seed push refuse them. The
operator's working tree (including uncommitted edits) is still overlaid onto the clone, so
in-flight changes continue to reach the build.
