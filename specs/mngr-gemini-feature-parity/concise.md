# mngr_gemini feature parity with mngr_claude

## Goal

Bring `libs/mngr_gemini` closer to the feature surface of `libs/mngr_claude`. Today `mngr_gemini` is a thin TUI agent with one hookimpl (`register_agent_type`) and common-transcript support; `mngr_claude` ships seven hookimpls, a full settings/credential management module, hook injection, session adoption, headless mode, skill-provisioned subtypes, a subagent proxy package, and a usage telemetry package.

Gemini CLI exposes the underlying primitives needed for most of these (see "Gemini CLI capability map" below). The gap is integration, not capability.

## Constraints

- Each PR below should be independently mergeable so the work can be parallelized across agents.
- Hook event names differ from Claude (`BeforeTool`/`SessionStart` vs `PreToolUse`/`SessionStart`); the mapping layer lives inside `gemini_config.py` and should not leak to callers.
- Auth on Gemini uses `GOOGLE_API_KEY`/`GEMINI_API_KEY` env vars and/or OAuth; there is no macOS-keychain sync equivalent (skip that piece of `mngr_claude`).
- Some Claude hooks have no Gemini equivalent (`Stop`, `UserPromptSubmit`); each PR must call out workarounds, not silently emulate.

## Gemini CLI capability map (for reference)

| Need | Gemini CLI mechanism |
|---|---|
| Settings | `~/.gemini/settings.json`, project `.gemini/settings.json`, `/etc/gemini-cli/settings.json` |
| Hooks | `hooks` top-level key in settings (event-specific configs); `hooksConfig` for system-wide enable/disable. Events confirmed in the published schema: `BeforeTool`, `AfterTool`, `BeforeAgent`, `AfterAgent`, `BeforeModel`, `AfterModel`, `BeforeToolSelection`, `SessionStart`, `SessionEnd`, `Notification`, `PreCompress` |
| Slash commands | TOML files in `~/.gemini/commands/` and project `.gemini/commands/` |
| Skills | Agent Skills standard, `~/.gemini/skills/` and `.gemini/skills/` |
| Subagents | Markdown + YAML frontmatter in `.gemini/agents/*.md` |
| MCP | `mcpServers` + `mcp.allowed` in settings |
| Memory file | `GEMINI.md` (project parent-walk + `~/.gemini/GEMINI.md`) |
| Permissions | Policy Engine, `tools.core`, `security.allowedTools`, approval modes `plan` < `default` < `auto_edit` < `yolo` (set via `general.defaultApprovalMode` -- note: NOT `general.approvalMode`, that key does not exist in the schema). CLI flags: `--approval-mode`, `-y/--yolo` |
| Folder trust | `security.folderTrust.enabled` (boolean, default `true`); `--skip-trust` CLI flag is per-session only |
| Telemetry | OpenTelemetry; local `.gemini/telemetry.log` or OTLP exporters |
| Non-interactive | `-p/--prompt` plus `-o text|json|stream-json` for parseable output; `-y` or `--approval-mode yolo` for tool auto-approval |
| Claude-format hook migration | `gemini hooks migrate --from-claude` (built-in subcommand) |

## Current state

Already implemented in `mngr_gemini`:

- `register_agent_type` hookimpl (returns `("gemini", GeminiAgent, GeminiAgentConfig)`)
- `GeminiAgent` extends `InteractiveTuiAgent` + `HasCommonTranscriptMixin`
- `--skip-trust` injected via default `cli_args` (workaround for trust dialog)
- `resources/common_transcript.sh` watching `~/.gemini/tmp/*/chats/*.jsonl`
- tmux-based input via `send_enter_and_poll_for_cleared_indicator` keyed on `"Type your message"` placeholder
- Reports `node` as expected process name (gemini does not override `process.title`)

Missing relative to `mngr_claude`: everything else (settings management, hook injection, the other six hookimpls, session adoption, headless variant, skill-provisioned subtypes, subagent proxy package, usage telemetry package).

## Proposed PR breakdown

PRs are ordered so dependency arrows point upward. PRs in the same tier are independent and can be picked up in parallel.

### Tier 1: foundation

**PR1 (LANDED): `gemini_config.py` — settings file management**
- Already merged into `mngr/gemini-feature-parity`. Subsequent PRs should rebase or branch off this commit. JSON shape produced by the builders was validated end-to-end against the published Gemini settings schema (smoke-tested against Gemini CLI 0.42.0).
- Provides path helpers, atomic write with `.bak` backup, malformed-JSON-tolerant reads, env-var interpolation, two hook-config builders (`SessionStart` readiness sentinel + `BeforeTool` wildcard auto-allow), `merge_hooks_config`/`hook_already_exists` dedup-preserving merge, and the placeholder `GeminiDirectoryNotTrustedError` class.

### Tier 2: lifecycle wiring (depends on PR1)

**PR2 (LANDED): hook injection (readiness sentinel + workspace trust + opt-in auto-allow)**
- LANDED on this branch: the `SessionStart` readiness hook from `build_readiness_hooks_config()` is wired into `GeminiAgent.provision()` so `mngr` can detect readiness from `$MNGR_AGENT_STATE_DIR/session_started` without polling the TUI.
- LANDED on this branch: the settings file is installed at the **system tier**, not the workspace tier. `provision()` writes `$MNGR_AGENT_STATE_DIR/plugin/gemini/system_settings.json` and `modify_env_vars()` sets `GEMINI_CLI_SYSTEM_SETTINGS_PATH` to point Gemini at it. This keeps the user's workspace and `~/.gemini/` completely untouched -- no `.gemini/` directory ever appears in the project, no merge with user files. Because mngr owns this file outright (it lives in the per-agent state dir), no merge logic or `.bak` backup is needed -- each provision run rewrites it from scratch. The earlier draft of this spec proposed writing to `<workspace>/.gemini/settings.json`; that approach was abandoned to avoid polluting the user's workspace.
- LANDED on this branch: the `--skip-trust` workaround was replaced with `GEMINI_CLI_TRUST_WORKSPACE=true` in the agent's environment, and `--skip-trust` has been dropped from the default `cli_args`. Smoke-testing against Gemini CLI 0.42.0 confirmed this is the correct path: workspace-level `<project>/.gemini/settings.json` hooks DO fire under the env var (`Hook registry initialized with 2 hook entries`), but are silently dropped under `--skip-trust` (`Hook registry initialized with 0 hook entries`). The env var is Gemini's documented headless-automation trust mechanism (https://geminicli.com/docs/cli/trusted-folders/).
- LANDED on this branch: opt-in `BeforeTool` permission auto-allow hook. `GeminiAgentConfig.auto_allow_permissions` (default `False`, mirroring `mngr_claude`) controls whether `_install_system_settings` also writes the wildcard `BeforeTool` hook from `build_permission_auto_allow_hooks_config()`. Preferred over `--approval-mode yolo`/`-y` because the hook survives admin policies that disable yolo mode (`security.disableYoloMode`) and shows up explicitly in Gemini's `--debug` hook-registry output. GitHub #20469 (non-interactive `autoEdit` mode bypassing some policy-engine rules) is documented inline on `build_permission_auto_allow_hooks_config`.
- REMAINING: investigate whether `gemini hooks migrate --from-claude` reformats anything for free before any further builder work.
- Acceptance: agent reaches ready state without `--skip-trust`; sentinel file appears within N seconds; `--debug` output shows `Hook registry initialized with N hook entries` where N > 0; when `auto_allow_permissions=True`, that registry count includes the `BeforeTool` entry and tool calls run without prompting.

**PR3: provisioning lifecycle wiring in `plugin.py`**
- Add hookimpls: `on_before_create` (preflight: settings.local.json gitignored, required env vars present), `agent_field_generators` (a `waiting_reason` field; map approval prompts → `PERMISSIONS`, idle → `END_OF_TURN`).
- Wire readiness sentinel from PR2 into the agent's `wait_for_ready()` path.
- Acceptance: `mngr create … gemini` runs preflight; `mngr list` shows the new `waiting_reason` field.

**PR4: `--adopt-session` CLI option**
- `register_cli_options` hookimpl on the `create` command; copy an existing `~/.gemini/tmp/<id>/chats/*.jsonl` into the new agent's session directory before launch.
- Honor Gemini's built-in `general.checkpointing` if it already covers the same ground — verify first before duplicating.
- Acceptance: adopting a real session resumes with prior transcript visible in the TUI.

**PR5: deploy + host-destroy hookimpls**
- `get_files_for_deploy` (stage `~/.gemini/settings.json`, `~/.gemini/GEMINI.md`, `~/.gemini/commands/`, `~/.gemini/agents/`, `~/.gemini/skills/` for scheduled deployments).
- `modify_env_vars_for_deploy` (set `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GOOGLE_GENAI_USE_VERTEXAI` when present).
- `on_before_host_destroy` (read session JSONLs from the volume API before deletion so they survive into the next agent's transcript history).
- Acceptance: scheduled remote deploys work end-to-end; destroyed host's session JSONL is preserved.

### Tier 3: variants (mostly independent, depends only on PR1 + PR2)

**PR6: headless Gemini agent**
- New `headless_gemini_agent.py` mirroring `headless_claude_agent.py`. Uses `gemini -p <prompt> -o stream-json` for one-shot prompts and parses the line-delimited JSON stream Gemini emits.
- Acceptance: a headless agent runs a single prompt to completion and returns the assistant text.
- Smoke-test confirmed `-o stream-json` exists in Gemini 0.42.0 (`gemini --help` lists `-o text|json|stream-json`), so unlike the earlier draft of this spec there is no open question about scraping vs. parsing.

**PR7: skill-provisioned base + code-guardian/fixme-fairy Gemini variants**
- `skill_agent_gemini.py` mirroring `skill_agent.py` for `~/.gemini/skills/` layout.
- `code_guardian_gemini_agent.py`, `fixme_fairy_gemini_agent.py` registering matching agent types.
- Acceptance: `mngr create … code-guardian-gemini` provisions the skill and the agent recognizes it.

### Out of scope (deferred)

The sibling-package work (`mngr_gemini_usage` mirroring `mngr_claude_usage`, `mngr_gemini_subagent_proxy` mirroring `mngr_claude_subagent_proxy`) is deferred. Both depend on PR1 but neither is on the critical path for the core agent reaching parity, and the telemetry path in particular needs design work (Gemini exposes OpenTelemetry, not statusline shims, so the integration shape differs).

## Out of scope

- macOS keychain credential sync (no analog needed for Gemini auth model).
- `Stop`-hook-based dispatch (no Gemini equivalent; closest is `SessionEnd` + `AfterAgent` and they fire too late for stop-hook orchestration). Document the gap; do not emulate.
- Monorepo-wide `GEMINI.md` auto-walk (Gemini only walks parents; this is a Gemini CLI limitation, not ours to fix here).

## Hand-off notes for implementing agents

- Each PR should add its own `changelog/<branch>.md` file (CI enforces this).
- Each PR should branch off `mngr/gemini-transcript` (not `main`). That line carries the common-transcript work this stack reuses; branching off `main` would conflict on `plugin.py` and `resources/`.
- Run `just test-quick libs/mngr_gemini` while iterating; `just test-offload` before marking ready.
- Read `libs/mngr_claude/imbue/mngr_claude/<analog>.py` before writing the corresponding Gemini file — most patterns transfer; the differences are at the Gemini config surface, not the mngr surface.
