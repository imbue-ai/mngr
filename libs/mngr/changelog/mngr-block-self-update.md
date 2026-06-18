Added a shared `AgentUpdatePolicy` (`AUTO` / `ASK` / `NEVER`) used by the agent plugins to govern an agent CLI's self-updater. The default resolves by context: `NEVER` for unattended (remote/deploy) agents, `ASK` where an agent implements an interactive update flow, otherwise `AUTO`.

Added shared installation helpers `extract_cli_semver` and `verify_pinned_cli_version` so agent plugins can verify an installed CLI matches a pinned version, erroring on a confirmed mismatch and skipping the check when the version is unparseable.
