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
| Hooks | `hooks` key in settings; events: `BeforeTool`, `AfterTool`, `BeforeAgent`, `AfterAgent`, `BeforeModel`, `AfterModel`, `BeforeToolSelection`, `SessionStart`, `SessionEnd`, `Notification`, `PreCompress` |
| Slash commands | TOML files in `~/.gemini/commands/` and project `.gemini/commands/` |
| Skills | Agent Skills standard, `~/.gemini/skills/` and `.gemini/skills/` |
| Subagents | Markdown + YAML frontmatter in `.gemini/agents/*.md` |
| MCP | `mcpServers` + `mcp.allowed` in settings |
| Memory file | `GEMINI.md` (project parent-walk + `~/.gemini/GEMINI.md`) |
| Permissions | Policy Engine, `tools.core`, `security.allowedTools`, approval modes `plan` < `default` < `autoEdit` < `yolo` |
| Telemetry | OpenTelemetry; local `.gemini/telemetry.log` or OTLP exporters |
| Non-interactive | `-p/--prompt` + `--approval-mode yolo` |

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

**PR1: `gemini_config.py` — settings file management**
- New module. Functions to read/write `~/.gemini/settings.json` (atomic), project `.gemini/settings.json`, and `~/.gemini/.env`. Helpers for the `hooks`/`mcpServers`/`tools.core`/`security.allowedTools`/`general.approvalMode`/`context.fileName` settings keys with merge-preserving semantics so we don't clobber user edits.
- Define typed dataclasses for the `hooks` block shaped by the Gemini hook spec (matcher, command, exit-code-2 = block).
- Error classes mirroring Claude's (`GeminiDirectoryNotTrustedError`, etc.) — `--skip-trust` is the current escape hatch; the proper fix is to write trust state into `settings.json` before launch.
- Acceptance: unit tests cover atomic write, merge with existing keys, malformed-JSON recovery, env-var interpolation in string values.
- Files: `libs/mngr_gemini/imbue/mngr_gemini/gemini_config.py` (+ test).
- No other-PR dependencies.

### Tier 2: lifecycle wiring (depends on PR1)

**PR2: hook injection (readiness + permission auto-allow)**
- Generate a `SessionStart` hook that touches a `session_started` sentinel so `mngr` can detect readiness without polling the TUI.
- Generate a `BeforeTool` hook (or use `--approval-mode yolo`) for headless-style auto-approval. Document the GitHub #20469 non-interactive policy-engine caveat.
- Replace the current `--skip-trust` workaround with a settings-managed `trustedFolders` entry seeded by `gemini_config.py`.
- Files: extend `gemini_config.py`, add `resources/readiness_hook.sh` (or similar tiny shell stub if we need stdout JSON output).
- Acceptance: agent reaches ready state without `--skip-trust`; sentinel file appears within N seconds.

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
- New `headless_gemini_agent.py` mirroring `headless_claude_agent.py`. Uses `gemini -p` for one-shot prompts; parses Gemini's output (currently no equivalent of Claude's JSON stream — likely have to capture stdout/stderr until they ship `--output-format json`).
- Acceptance: a headless agent runs a single prompt to completion and returns the assistant text.
- Open question to surface in the PR description: does Gemini CLI emit a structured stream we can parse, or do we have to scrape? Search recent issues before starting.

**PR7: skill-provisioned base + code-guardian/fixme-fairy Gemini variants**
- `skill_agent_gemini.py` mirroring `skill_agent.py` for `~/.gemini/skills/` layout.
- `code_guardian_gemini_agent.py`, `fixme_fairy_gemini_agent.py` registering matching agent types.
- Acceptance: `mngr create … code-guardian-gemini` provisions the skill and the agent recognizes it.

### Tier 4: sibling packages (independent of each other, depend on PR1)

**PR8: `libs/mngr_gemini_usage`**
- New package mirroring `mngr_claude_usage`. Gemini exposes OpenTelemetry, not statusline shims, so the approach differs: configure `telemetry.target = "local"` + `telemetry.outfile = .gemini/telemetry.log`, then tail and convert OTel GenAI semantic-convention metrics (`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`) into `events.jsonl`.
- Acceptance: per-agent cost/token snapshots appear in `events.jsonl` after a Gemini turn.

**PR9: `libs/mngr_gemini_subagent_proxy`**
- New package mirroring `mngr_claude_subagent_proxy`. Register `mngr-proxy-gemini-child` agent type. Use Gemini's `BeforeAgent`/`AfterAgent` hooks for subagent lifecycle (no `UserPromptSubmit` analog — call this gap out in the PR).
- Acceptance: a `gemini` agent can spawn a `mngr-proxy-gemini-child` and round-trip a request.

## Out of scope

- macOS keychain credential sync (no analog needed for Gemini auth model).
- `Stop`-hook-based dispatch (no Gemini equivalent; closest is `SessionEnd` + `AfterAgent` and they fire too late for stop-hook orchestration). Document the gap; do not emulate.
- Monorepo-wide `GEMINI.md` auto-walk (Gemini only walks parents; this is a Gemini CLI limitation, not ours to fix here).

## Hand-off notes for implementing agents

- Each PR should add its own `changelog/<branch>.md` file (CI enforces this).
- Each PR should branch off `main` (not this branch — this branch only holds the spec).
- Run `just test-quick libs/mngr_gemini` while iterating; `just test-offload` before marking ready.
- Read `libs/mngr_claude/imbue/mngr_claude/<analog>.py` before writing the corresponding Gemini file — most patterns transfer; the differences are at the Gemini config surface, not the mngr surface.
