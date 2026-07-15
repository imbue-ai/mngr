Added and maintained the planning documents for the minds "inspirations"
feature under `blueprint/minds-inspirations/`: the implementation plan and a
concise feature prompt. Inspirations let a running mind publish a clean,
bootable snapshot of the apps it built to a new GitHub repo, and let another
mind adapt one into itself. The plan records the full design evolution from
live testing -- assembly delegated to a launch-task worker on an isolated
worktree with a strict no-merge-back invariant, an inline-chat scope gate and
post-assembly confirmation, latchkey GitHub permissioning end-to-end (REST API
plus a git push through the latchkey gateway), a single-commit publish that
leaks no intermediate state, deterministic base resolution, published-version
modifications, a bespoke-thumbnail gate, a two-scanner (betterleaks +
kingfisher) secret gate, and an inspiration-describing README. The
implementation itself lives in the default-workspace-template repo on the
companion branch of the same name.
