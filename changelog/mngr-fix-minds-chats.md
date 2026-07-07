Investigation note for the minds "+ New Chat" slowness on the `engman` workspace. No
code changes; spec only. Captures the timeline, where the agent actually came up
(within ~1s), and why the dockview tab stayed in "Creating..." for ~6 minutes (the
`mngr create` subprocess most likely hung between `emit_discovery_events_for_host`
returning and click-cmd exit on the first chat-create after workspace bootstrap, so
`_run_creation` never reached the `_agents[agent_id] = ...` assignment or the
`proto_agent_completed` broadcast; the observe pipeline was healthy throughout).
Recommends adding a structured "mngr create returned (rc=N, elapsed=T)" log at the
end of `_run_creation` so future hangs are visible, plus defensive cleanups:
wrapping `_handle_observe_output_line` in a try/except, fixing `--events-dir` being
silently ignored in `--discovery-only` mode, and backing off
`ChatPanel.fetchScreenCapture` so a 404 does not produce a polling storm.
