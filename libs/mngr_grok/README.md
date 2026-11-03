# imbue-mngr-grok

The `grok` agent-type plugin for [`mngr`](https://pypi.org/project/imbue-mngr/): runs xAI's
[Grok Build](https://github.com/xai-org/grok-build) CLI (`grok`) as an mngr agent that mngr can
monitor (RUNNING/WAITING), message, transcript, stop and resume, clone, adopt and isolate per agent.

This plugin runs Grok Build's own agent, with its tools and subagents. To use Grok models in
another harness, use the `opencode` or `pi-coding` agent type with your xAI credential instead.

## Installation

```bash
uv tool install imbue-mngr --with imbue-mngr-grok
# or, on an existing install:
mngr plugin add imbue-mngr-grok
```

Install Grok Build from npm (Node 20 or newer):

```bash
npm install -g @xai-official/grok
grok --version    # grok 1.0.62 (<build hash>)
```

- The plugin is tested with Grok Build 1.0.62. Nothing is pinned by default; set `version = "1.0.62"` to install and enforce that version.
- Do not install the community `grok-cli` (`superagent-ai/grok-cli`): it is a different program that installs a binary also named `grok`, and this plugin does not work with it. Provisioning fails when `grok --version` prints something other than Grok Build's `grok X.Y.Z (<build hash>)`.
- When `grok` is missing, provisioning installs it with `env -u GROK_HOME npm install -g @xai-official/grok` (with `@<version>` when `version` is set) and fails unless the installed `grok --version` runs: on a local host after you confirm or with `mngr create --yes`, and on a remote host when `is_remote_agent_installation_allowed` is true (the default).
- The agent's host also needs `python3` 3.9 or newer (for mngr's hook handler and transcript scripts) and git 2.13 or newer.

## Signing in

- **Shared login (default, `share_auth = true`).** Each agent's `GROK_AUTH_PATH` points at your credential file (`$GROK_AUTH_PATH`, else `${GROK_HOME:-~/.grok}/auth.json`), so one `grok login` signs in every agent on that host and token refreshes are shared. A `GROK_AUTH_PATH` set in the agent's environment wins.
- **API key.** Pass `XAI_API_KEY` to the agent: `mngr create my-agent grok --pass-env XAI_API_KEY`. A stored login takes precedence over the key, so set `share_auth = false` to use the key while you are logged in.
- **API key only (`share_auth = false`).** No stored login reaches the agent: neither your credential file nor a `GROK_AUTH_PATH` in its environment. The agent needs `XAI_API_KEY`; without it, grok starts on its login screen and the create fails.
- **Remote hosts.** The shared credential is the remote user's `~/.grok/auth.json`, not your local one. Run `grok login --device-auth` on the remote host once, or pass `XAI_API_KEY`.

A login needs an xAI subscription that includes Grok Build; an account without one gets a paywall screen. API keys are not subject to the paywall.

## Usage

```bash
mngr create my-agent grok
mngr message my-agent -m "Summarize the open TODOs in src/"
mngr transcript my-agent
```

Arguments after `--` are passed to `grok`.

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.grok_reviewer]
parent_type = "grok"
model = "grok-4.6"

[agent_types.grok_reviewer.settings_overrides.permission]
allow = ["Bash(git *)", "Read(src/**)"]
deny = ["Bash(rm *)"]
```

`settings_overrides` is a patch onto the `config.toml` mngr renders for the agent. A bare key assigns, and mngr refuses an assignment that would drop a table it writes; declare a merge in `__mngr_merge` instead (mngr's config README has the details):

```toml
[agent_types.grok_reviewer.settings_overrides.ui]
prompt_suggestions = true
[agent_types.grok_reviewer.settings_overrides.__mngr_merge]
"ui" = "extend"   # keeps the permission_mode mngr writes
```

Configuration is rendered when the agent is created; `mngr create <name> --reuse --update` renders it again.

### Fields

- `command` (default `"grok"`): the command that runs Grok Build; it may carry a launcher prefix. `cli_args` and the arguments after `--` follow it.
- `model` (default unset): launches grok with `-m <model>`. Unset keeps the account's default, or the model chosen inside the agent with `/model`.
- `reasoning_effort` (default unset): launches grok with `--reasoning-effort <value>`. Unset keeps grok's default, or the effort chosen inside the agent.
- `append_system_prompt` (default `[]`) and `output_style` (default unset): the agent's role, joined in that order and passed with `--rules`. `output_style` names a file in the work dir's `.agents/output-styles/` by its `name:` frontmatter. The role is limited to 100,000 bytes.
- `auto_allow_permissions` (default `false`): when true, grok approves every tool call (`[ui] permission_mode = "always-approve"`), and deny rules still apply. When false, the mode is written as `"ask"`.
- `auto_dismiss_dialogs_at_startup` (default `false`): trust the work dir for grok without asking (see [Folder trust](#folder-trust)).
- `settings_overrides` (default `{}`): folded last onto the agent's `config.toml`; layers from several config scopes accumulate. Its `[permission]` table (`allow`, `ask`, `deny`) is the agent's permission policy.
- `share_auth` (default `true`): see [Signing in](#signing-in).
- `isolate_from_claude_config` (default `true`): see [Claude Code isolation](#claude-code-isolation).
- `pin_privacy_settings` (default `true`) and `memory` (default `false`): see [Privacy and memory](#privacy-and-memory).
- `check_installation` (default `true`): install grok when it is missing and check that `grok --version` reports Grok Build; false assumes it is present.
- `version` (default unset): the grok version to install; provisioning checks it against `grok --version`, per `version_mismatch`.
- `version_mismatch` (default `ERROR`): what provisioning does when the installed grok does not report the pinned `version`: `ERROR` fails the create, `WARN` logs a warning and continues on the installed grok.
- `update_policy` (default unset, which means `NEVER`): `NEVER` sets `GROK_DISABLE_AUTOUPDATER=1` (unless the agent's environment sets it) and `[cli] auto_update = false`, so the installed build stays put. `AUTO` leaves grok's updater on; `ASK` behaves like `AUTO`.
- `emit_common_transcript` (default `true`): emit the transcript `mngr transcript` reads.
- `preserve_on_destroy` (default `true`): see [Sessions](#sessions).

## How a grok agent behaves

Each agent runs with its own `GROK_HOME` under `$MNGR_AGENT_STATE_DIR/plugin/grok/home`. Your own `~/.grok` configuration (MCP servers, skills, rules, hooks) is not carried in. What does carry over is your folder-trust grant for the work dir and, under `share_auth`, your credential and the xAI notices you accepted.

### Lifecycle

mngr's hooks, in the agent's grok home, mark the agent RUNNING from a prompt's submission until grok records that turn's `turn_completed` in its session log, and while any subagent runs. The turn ends there rather than at grok's `Stop` hook, because a blocking `Stop` hook keeps the turn going; the transcripts are flushed before the agent reports WAITING.

- A background subagent that finishes keeps the agent RUNNING through grok's follow-up turn for it.
- A background shell does not keep the agent RUNNING; when it finishes, grok's follow-up turn makes the agent RUNNING again.
- A turn cancelled with Ctrl+C ends at grok's `StopCancelled` hook, which can come before the transcripts are flushed. A turn grok ends without reporting it (such as one cancelled with Ctrl+C before its first reply, whose prompt grok puts back in the input box) ends at grok's idle notification, about a minute later, or, when mngr pressed that Ctrl+C while only one top-level session had a turn running, as soon as mngr has cleared what grok put back (5 seconds after the press when grok first shows the input box empty).
- `mngr list` shows `waiting_reason`: `PERMISSIONS` while a permission card is open, `END_OF_TURN` otherwise. The batched listings of the Modal and VPS providers show an agent waiting on a permission card as RUNNING.

### Messages and keys

- A message sent while grok is busy is queued by grok and runs after the current turn. `mngr message` succeeds once the queued row or the prompt's submission names that message.
- A slash command is confirmed the same way or, for one that starts a session such as `/new`, once that session starts. One that leaves neither kind of evidence is reported sent after a short wait, with a warning.
- A message starting with `!` runs as a shell command in grok's shell mode, which fires no hook, so like a slash command that leaves no evidence it is reported sent after a short wait, with a warning.
- mngr presses Enter again only while grok's footer shows the text still in the composer.
- mngr never types into a screen other than the session composer: the login, folder-trust, consent and paywall screens, or a permission card. Keys act on those screens, so `mngr create` and `mngr message` fail with an error naming the screen instead (see [Troubleshooting](#troubleshooting)). A create that fails this way removes a new agent and keeps one it updates (`--reuse --update`).
- mngr refuses to press Ctrl+O (saves always-approve), Shift+Tab (cycles the permission mode, which can save always-approve), Ctrl+Q (quits grok; `mngr stop` stops the agent), Ctrl+Enter, or Enter while the footer shows `Enter:send now` (both cut the running turn short), or `1` on a permission card or Enter while its always-approve option is selected (always-approve for every tool).
- mngr presses Ctrl+C only to cancel a running top-level turn: a turn is in flight (a background subagent or the follow-up turn grok is about to start for one does not count), a fresh look at the pane shows its `Ctrl+c:cancel` hint, and the composer is empty. Otherwise it refuses, because on an idle grok Ctrl+C arms quit (a second press quits) and with text in the composer it only clears the text. Ctrl+C presses are at least 1.5 seconds apart, and the conditions are checked after that wait. When only one top-level session has a turn running, mngr then waits up to 5 seconds for grok to end it. A turn grok drops instead (one cancelled before its first reply) mngr ends itself: at once after clearing what grok put back in the input box, the prompt or its `[Pasted: N lines]` chip, with one more Ctrl+C, or at the 5-second mark when grok first shows the input box empty, leaving anything typed into it meanwhile. When that clear or mngr's end of the turn does not take, the press fails and mngr leaves the turn recorded (the agent reads RUNNING). A turn grok still runs 5 seconds after the press is left for grok to end. When turns run in more than one top-level session, mngr cannot tell which one the Ctrl+C reached, so the press returns at once and a turn grok drops ends at grok's idle notification.

### Claude Code isolation

With `isolate_from_claude_config = true` (the default), grok imports none of your Claude Code or Cursor configuration: hooks, plugins, skills, rules, agents, MCP servers and sessions (and none of your Codex sessions). Agent definitions in `~/.claude/agents/*.md` and `~/.grok/agents/*.md` are turned off by name; one added later stays selectable until `mngr create <name> --reuse --update`.

- Still loaded: `AGENTS.md` and `CLAUDE.md` in the work tree once the folder is trusted, and `~/.agents/skills`.
- **Still applied:** your Claude Code `permissions.deny` rules in `~/.claude/settings.json` still deny tools inside a grok agent, and can block a tool the agent needs.

With `isolate_from_claude_config = false`, grok keeps the Claude Code and Cursor skills, rules, agents and MCP servers that are not part of a Claude Code plugin, the Claude Code, Cursor and Codex sessions, and the agent definitions in `~/.grok/agents`, while every Claude Code and Cursor hook stays off. Each plugin in your Claude `enabledPlugins` is disabled by name, which also drops its skills and agents, and the `env` block of your Claude settings reaches grok's tool processes.

### Folder trust

Grok asks before trusting a folder that holds project configuration (`AGENTS.md`, `CLAUDE.md`, `.mcp.json`, `.envrc`, and so on); trusting it lets grok load the folder's MCP servers, LSP config, `.envrc`, project hooks and plugins. mngr records the trust before launch, for the key grok uses: the repository root (the main checkout for a linked worktree), otherwise the work dir, with symlinks resolved. It does so only with consent:

1. Your own `${GROK_HOME:-~/.grok}/trusted_folders.toml` already trusts the key: mngr copies the grant.
2. `auto_dismiss_dialogs_at_startup = true` or `mngr create --yes`: mngr trusts it.
3. An interactive terminal: mngr asks, defaulting to no; declining aborts the create.
4. Otherwise the create fails, naming `--yes` and `auto_dismiss_dialogs_at_startup`.

A new grant is also written to your own `trusted_folders.toml`, so later agents for that folder, and your own `grok` runs there, do not ask again. A work dir that is `$HOME` or `/` gets grok's provisional trust, and nothing is recorded. mngr never turns grok's folder trust off.

### Privacy and memory

With `pin_privacy_settings = true` (the default), the agent's `config.toml`, its `requirements.toml` (which takes precedence over xAI's server settings and the environment) and its environment turn off grok's telemetry, trace upload, Mixpanel analytics, feedback, campaigns and codebase upload, and set `[harness] disable_workspace_teleport`. Whatever the setting, mngr turns off grok's leader mode, tips, prompt suggestions and changelog fetch, sets `[ui] cancel_subagents_on_turn_cancel = "always_continue"` so Ctrl+C cancels a turn without asking about running subagents and leaves them running, and an agent whose `settings_overrides` defines a model with its own credential (`base_url`, `api_key` or `env_key` in a `[model.<id>]` table) does not fetch xAI's remote settings.

mngr also writes `[privacy] privacy_banner_acked` into every agent's `config.toml`, whatever the setting (a re-render keeps the time already there). That records grok's privacy banner, one of the screens grok can show before the session view, as acknowledged on your behalf. mngr does not change the account's retention setting.

> **Note:** the privacy pins are not yet verified against xAI's server settings with a real subscription login. Nor are the privacy banner's text, which accounts get it, or whether `privacy_banner_acked` keeps it away: no captured session has shown it.

`memory = false` (the default) writes `[memory] enabled = false`, so no server default turns grok's cross-session memory on. With `memory = true`, grok keeps its memory store in the agent's grok home; clones and adoptions start without it.

### Sessions

- **Resume.** mngr chooses each agent's session id. `mngr stop` then `mngr start` resumes the agent's latest session; `/new` inside grok makes the new session the one to resume. If grok fails before its session starts (for example, when the session cannot be resumed), the launch starts a fresh session.
- **Clone and adopt.** `mngr create <name> grok --from <agent>` copies the source agent's current session into the new agent, and `--adopt <session id or session directory>` copies one from `~/.grok/sessions`, a live local grok agent, or a preserved one. The first launch forks the copy into the agent's own session. A fork keeps its source session's role, because grok fixes a session's system prompt when the session is created; the agent's own role applies to the sessions it starts later. A clone or adoption fails for a work dir whose URL-encoded path exceeds 255 bytes.
- **Preservation.** With `preserve_on_destroy = true`, `mngr destroy` first copies the agent's transcripts and grok sessions (and its memory store when `memory = true`) to `<local_host_dir>/preserved/`, where `--adopt <session id>` finds them.

## Troubleshooting

- **"grok has no usable credential"** (the login screen): run `grok login` on the agent's host (`grok login --device-auth` on a remote host), or pass `XAI_API_KEY`, then run the failed `mngr create` again, or `mngr create <name> --reuse --update` for an agent that exists. With `share_auth = false` only `XAI_API_KEY` works: pass it when you create the agent (`--pass-env XAI_API_KEY`).
- **"grok is asking whether to trust the agent's work dir"** (the trust dialog): mngr records trust when it creates the agent, so this is a bug; report the work dir's layout. An agent that exists can answer the dialog through `mngr connect <agent>`.
- **"grok shows a notice from xAI"** (a consent notice): run `grok` yourself and accept it, then run the failed `mngr create` again, or `mngr create <name> --reuse --update` for an agent that exists, so the agent copies your answer; with `share_auth = false` nothing is copied. An agent that exists can also accept it through `mngr connect <agent>`.
- **"the account has no subscription that includes Grok Build"** (the paywall): sign in with a qualifying account, or use `XAI_API_KEY` with `share_auth = false`. Do not press keys on this screen: `l` logs out and deletes the credential file, which under `share_auth` is your own.
- **A hook failure line in grok's TUI** (`◆ <event> hook (global/mngr) failed, ignored: …`): mngr's hook handler could not run, so the agent's RUNNING/WAITING state is unreliable. Check that `python3` 3.9 or newer is on the host's `PATH`, then run `mngr create <name> --reuse --update` to reinstall the handler. Failed runs are also recorded in `$MNGR_AGENT_STATE_DIR/events/grok/hook_failures/events.jsonl`, except those of the `Notification` and `SubagentStart` hooks, which grok leaves out of its session log.
- **`$MNGR_AGENT_STATE_DIR/logs/grok_hooks.log`** has one line per run of mngr's handler, `<time> event=<event> session=<id> prompt=<id> <outcome>`. An error inside the handler reads `error:<exception>` and is recorded only here, because grok sees that run succeed; a handler that could not run writes no line. The launch script logs to `$MNGR_AGENT_STATE_DIR/events/logs/grok_launch/events.jsonl`.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
