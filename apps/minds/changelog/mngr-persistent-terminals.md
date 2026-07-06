- Added `apps/minds/docs/persistent-terminals.md`, a short doc explaining the
  lifecycle of the dockview terminals (in-memory tmux sessions that survive
  closing a tab, reloading, and terminal-service restarts, but not a container
  restart) and how a user could opt in to on-disk persistence. The
  in-workspace terminal banner links here.

- The terminal feature itself (named, in-memory-persistent tmux sessions,
  close-vs-destroy, a reattach list, live tab-title tracking, and the banner)
  lives in the forever-claude-template repo (system_interface + `run_ttyd.sh` +
  tmux config); this monorepo change is just the linked doc.
