- `dev`: update `specs/detached-destroy-flow/spec.md` to describe deriving
  a destroying agent's status from the destroy wrapper's recorded exit
  code (a new atomic `result` file) instead of from the lagging discovery
  cache, plus the reuse-safe PID check (`process_start`) and the
  landing-page finalize timing that keeps "Destroying…" visible until
  discovery drops a succeeded agent.
