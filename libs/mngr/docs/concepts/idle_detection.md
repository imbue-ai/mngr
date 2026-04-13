# Idle Detection

Hosts are automatically paused when idle to save resources (critical for cloud providers where running agents cost money).

A host is considered "idle" if there has been no "activity" for a configured timeout period. If multiple agents share a host, activity from any agent may keep the host running.

In addition to idle timeout, hosts are automatically **stopped** (not just paused) when all agent tmux sessions have exited. This is detected by checking for tmux sessions matching the configured prefix (stored as `tmux_session_prefix` in `data.json`). When no matching sessions remain and a grace period has elapsed, the host shuts down with `stop_reason=STOPPED`. This ensures hosts are promptly cleaned up when all agents finish their work.

What counts as "activity" is highly configurable. Run `mngr limit --help` [future] to see the available flags.

Any of the following can be considered activity:

- user input [future] like keystrokes (terminal and web) and mouse movement (web). Requires accessing the agent via `mngr connect` (terminal) or using the `user_activity_tracking_via_web` plugin [future]. See [User Input Tracking](#user-input-tracking) below for details.
- agent output (supported by most agents)
- active SSH connections
- agent process being alive
- host creation
- host startup

For convenience, there are several **idle modes** that bundle common configurations together. This table shows what activity counts for each mode:

| Mode           | User Input | Agent Output | SSH | Agent Creation | Agent Startup | Boot | Agent Process |
|----------------|:----------:|:------------:|:---:|:--------------:|:-------------:|:----:|:-------------:|
| `io` (default) |     ✓      |      ✓       |  ✓  |       ✓        |       ✓       |   ✓  |               |
| `user`         |     ✓      |              |  ✓  |       ✓        |       ✓       |   ✓  |               |
| `agent`        |            |      ✓       |  ✓  |       ✓        |       ✓       |   ✓  |               |
| `ssh`          |            |              |  ✓  |       ✓        |       ✓       |   ✓  |               |
| `create`       |            |              |     |       ✓        |               |      |               |
| `boot`         |            |              |     |                |               |   ✓  |               |
| `start`        |            |              |     |                |       ✓       |   ✓  |               |
| `run`          |            |              |     |       ✓        |       ✓       |   ✓  |       ✓       |
| `disabled`     |            |              |     |                |               |      |               |

The "create", "boot" and "run" modes are most useful for scripting (which correspond to "time since the agent was created", "time since the host came online" and "time since the agent exited" respectively). The "start" mode is "time since the agent started" and is useful for limiting agent lifetime when scripting.

## Trustworthiness of activity reporting

Only the following activity reports are trustworthy:
- agent creation
- host boot

Everything else can be manipulated by a malicious or buggy agent (including e.g. changing the clock).

This means that, if running an untrusted agent, you should only use the "create" or "boot" idle modes to ensure that the agent cannot prevent stopping by faking activity. You should also use a provider that enforces host lifetime limits externally (e.g., Modal's sandbox timeout), which guarantees the host will be stopped regardless of what happens inside it.

## User Input Tracking [future]

User input tracking requires either terminal access via `mngr connect` or web access via the [user_activity_tracking_via_web plugin](../core_plugins/user_activity_tracking_via_web.md), [future]).

`mngr connect` reports activity via an SSH heartbeat (writing to `$MNGR_HOST_DIR/activity/ssh` every 5 seconds while connected). Keystroke-level tracking that writes to `$MNGR_HOST_DIR/activity/user` is planned. [future]

For web interfaces, the ideas is that [user_activity_tracking_via_web plugin](../core_plugins/user_activity_tracking_via_web.md) would inject JavaScript that tracks both mouse movements and keystrokes, reporting activity back to the agent in the same way.

Note that the only "tracking" happening is the most recent timestamp--there is no logging of actual keystrokes or mouse movements, and nothing except the most recent time is stored.
This mechanism is necessary in practice because you really don't want an agent to stop while you're actively using it.

## Agent Output Tracking

Most agents should be configured to write to the special file `$MNGR_HOST_DIR/agents/{agent_id}/activity/agent` whenever they produce output or are thinking.

You can modify this file yourself in scripts if you want to signal agent activity. The simplest way is to just `touch` the file.

## Activity File Format

Activity files by convention contain JSON with a `time` field (milliseconds since Unix epoch) and optional debugging fields. However, the **file's modification time (mtime) is the authoritative timestamp** for idle detection.

This means simple scripts can just `touch` the activity file without writing JSON:

```bash
# Signal activity - mtime will be updated
touch "$MNGR_HOST_DIR/activity/user"
```

Or write JSON for better debugging:

```bash
TIME_MS=$(($(date +%s) * 1000))
printf '{\n  "time": %d\n}\n' "$TIME_MS" > "$MNGR_HOST_DIR/activity/user"
```
