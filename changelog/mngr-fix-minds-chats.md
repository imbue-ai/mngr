Investigation note for the minds "+ New Chat" slowness on the `engman` workspace. No
code changes; spec only. Captures the timeline, where the agent actually came up
(within ~1s), and why the dockview tab stayed in "Creating..." for ~6 minutes (the
workspace-server's `agent_manager._agents` did not reflect the new agent because the
`mngr observe --discovery-only` reader path most likely died on an unhandled
exception in `_handle_observe_output_line`). Recommends wrapping that handler in a
try/except, watchdogging the reader thread, fixing `--events-dir` being silently
ignored in `--discovery-only` mode, and backing off `ChatPanel.fetchScreenCapture`
so a 404 does not produce a polling storm.
