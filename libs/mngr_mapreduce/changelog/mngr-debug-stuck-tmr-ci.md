Move post-finalize ``stop_agent_on_host`` calls off the polling loop's main thread.

When a mapper publishes outputs and the underlying remote sandbox (e.g. a Modal
sandbox) has already been torn down, the SSH ``stop_agents`` call blocks on the
kernel's TCP retransmit timeout -- observed at ~16 minutes per call. The
previous synchronous code path serialized the polling loop on those waits,
which left ~50 of 80 mappers unfinalized when a TMR run hit the 4h GHA cap.

Changes:
- Introduce ``_BackgroundStopper`` in ``orchestration.py``: a small
  context-manager helper that spawns an ``ObservableThread`` per stop and
  context-exits with a bounded 60s drain. Anything that escapes
  ``stop_agent_on_host``'s own ``(MngrError, HostError)`` catch is still
  logged via ObservableThread's error logger, but suppressed on join so a
  rogue stop can't crash the drain.
- ``launch_and_poll_mappers`` and ``wait_for_reducer`` now hold a stopper
  for the lifetime of their polling loop and route the post-finalize and
  per-agent-timeout stop calls through it instead of calling
  ``stop_agent_on_host`` synchronously. The mapper-finalize helper takes
  the stopper as a new parameter.

No changes to mngr core; this is a pure orchestrator-side workaround.
