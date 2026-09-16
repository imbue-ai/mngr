# imbue-mngr-autocompact

Automatic context compaction plugin for [mngr](https://github.com/imbue-ai/mngr) that manages context compaction for conversational agents implementing `HasCompactionMixin` (e.g., Claude Code agents).

## Overview

In LLM-based coding agents using Claude Code and similar architectures, prompt cache TTL remains warm for a finite lifespan (e.g., 60 minutes for Anthropic Claude models). When an agent sits idle, its prompt cache expires, causing subsequent turns to incur full cache-miss latency and token costs.

`mngr-autocompact` triggers context compaction (such as Claude's `/compact`) shortly before cache expiration (e.g., after 57 minutes of idle time), or right before delivering a new prompt to a stale agent. This drastically reduces prompt token usage and latency while keeping concise summary context in the conversation.

The plugin is completely stateless: it does not rely on long-lived daemon threads or in-memory global state, making it reliable in standard, short-lived `mngr` CLI workflows.

## Configuration

Configure the plugin in `settings.toml` under `[plugins.autocompact]`, or via `mngr config`:

```toml
[plugins.autocompact]
# Compaction mode: "disabled" (default), "proactive_timer", or "on_next_prompt"
mode = "proactive_timer"

# Minutes before cache expiration to trigger compaction (default: 3)
epsilon_offset_minutes = 3

# Minimum context size in tokens required to trigger compaction (default: 100000; set to 0 to disable)
min_context_tokens = 100000

# Optional override for model cache TTL in minutes (uses agent's reported TTL if omitted)
# cache_ttl_minutes = 60
```

You can also configure options from the CLI:

```bash
# Enable proactive periodic compaction
mngr config set plugins.autocompact.mode proactive_timer

# Set token threshold to 80k
mngr config set plugins.autocompact.min_context_tokens 80000
```

### Modes

* **`proactive_timer`**: Enables staleness checks when running `mngr autocompact check` or `mngr autocompact run`. An agent is considered stale when it is running, idle, and its idle time $\ge (T_{\text{cache\_ttl}} - \epsilon)$.
* **`on_next_prompt`**: Automatically checks staleness before delivering a message to an agent (hooked into `mngr message`). If the agent is idle past its cache TTL, compaction is triggered immediately before the message is sent.
* **`disabled`**: Disables automatic compaction checks.

## Usage

### CLI Commands (`mngr autocompact`)

The plugin registers the `autocompact` command group on `mngr` with two subcommands: `check` (inspect only) and `run` (execute compaction):

```bash
# Inspect which agents need compaction without triggering it
mngr autocompact check my-agent
mngr autocompact check --all

# Run compaction on stale agents
mngr autocompact run my-agent
mngr autocompact run --all

# Output results as JSON
mngr autocompact check --all --format json
mngr autocompact run --all --format json
```

Example JSON output for `check`:
```json
{"agents": ["agent-1", "agent-2"]}
```

Example JSON output for `run`:
```json
{"compacted": ["agent-1", "agent-2"]}
```

### Scheduling Proactive Compaction

Because `mngr` CLI processes are short-lived, proactive compaction is driven by running `mngr autocompact run --all` periodically via your preferred scheduler (cron, systemd timer, or background task).

For example, to compact stale agents every 5 minutes with cron:

```cron
*/5 * * * * mngr autocompact run --all -q
```

Agents are only compacted once per idle epoch (until a new turn completes), ensuring that frequent checks never issue redundant compactions.

## Agent Compatibility

The plugin is agent-agnostic and interacts with any agent implementing the `HasCompactionMixin` interface (`imbue.mngr.interfaces.agent.HasCompactionMixin`), including:
- `request_compaction(instructions=None)`: sends `/compact` (optionally with instructions) or the agent's native compaction command.
- `get_cache_ttl_minutes()`: reports prompt cache TTL (default: 60 minutes for Claude).
- `get_context_tokens()`: reports context token count from recent transcript turns.
- `get_idle_since()`: reports when the agent finished its latest turn.
