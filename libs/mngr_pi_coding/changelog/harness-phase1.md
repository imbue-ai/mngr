- Provisioning copies the home npm extension install into the per-agent dir, so pi's first boot no longer spends 45-55s in npm before the readiness sentinel (first boot now matches a warm boot, ~8-13s).

- The lifecycle extension writes the session file and model state BEFORE the readiness sentinel, so everything the chat surface needs at first paint is on disk when readiness fires; a failed model-state write now withholds readiness instead of signaling a half-ready agent.
