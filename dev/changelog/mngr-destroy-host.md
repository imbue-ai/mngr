- `dev`: update `specs/detached-destroy-flow/spec.md` to describe deriving
  a destroying agent's status from the destroy wrapper's recorded exit
  code (a new atomic `result` file) instead of from discovery host state,
  the reuse-safe PID check (`process_start`), the whole-host fanout (no
  single-agent path), and the landing page rendering destroy records even
  for agents discovery no longer lists (so a failed teardown can't become
  an invisible orphan).
