# Group 2: git + coding-agent infrastructure

---

## 1. commits

### 1.1 Canonical definition

A git commit as used in the mngr/minds system. The term appears in two distinct operational contexts that must be kept separate:

**Context A — agent work branch commits (user-visible work)**: commits Claude Code makes on the `mngr/<agent_name>` branch inside the agent container. These are the primary outputs of background agent work and the subject of branch transfer, push/pull, and changelog requirements.

**Context B — infrastructure commit constraints**: a set of operations the system actively *prevents* the agent from performing: rebase, `commit --amend`, `commit --fixup`. These constraints are enforced by a PreToolUse Claude Code hook.

### 1.2 All usages

- `libs/mngr/imbue/mngr/utils/git_utils.py`: `get_head_commit(path, cg)` returns the `HEAD` SHA string (or None); `count_commits_between(path, base_ref, head_ref, cg)` counts reachable commits.
- `libs/mngr/imbue/mngr/utils/git_utils.py`: `is_ancestor(path, ancestor_commit, descendant_commit, cg)` tests commit ancestry. (These helpers now take an explicit repo `path` and a `ConcurrencyGroup` rather than a `ctx`.)
- `libs/mngr/imbue/mngr/api/git.py`: the commit history of an agent's branch is what `git_push()` / `git_pull()` transfer. With no explicit refspec, `git_push` pushes `<local_current_branch>:<remote_current_branch>` to the constructed URL (it no longer hard-codes `HEAD:<branch>`).
- `.external_worktrees/forever-claude-template/scripts/git_hooks/post-commit`: the single git hook fires after every commit, auto-pushing the branch to `origin` in the background when `GH_TOKEN` is set.
- `.external_worktrees/forever-claude-template/scripts/claude_prevent_commit_rewrite.sh`: PreToolUse hook that blocks `git rebase`, `git pull --rebase`, `git commit --amend`, `git commit --fixup` with exit code 2 (hard block). The hook does NOT prevent ordinary commits.
- `libs/mngr/imbue/mngr/plugins/hookspecs.py`: mngr's pluggy hooks `on_before_provisioning` / `on_after_provisioning` fire around agent setup, not around git commits.
- `CLAUDE.md` (repo root): "NEVER amend commits or rebase — always create new commits."

### 1.3 Competing definitions / terminology variants

None structurally, but two very different *roles* for commits:

| Role | Where enforced |
|---|---|
| Agent work output commit | Created by Claude Code normally; pushed via `post-commit` hook |
| Forbidden commit mutation | Blocked by `claude_prevent_commit_rewrite.sh` PreToolUse hook |

### 1.4 Ambiguities

- The words "commit" in CLAUDE.md refer to mngr developer workflow commits (the monorepo itself), while the FCT hook prevents commit mutation inside agent containers. Same word, two different scopes. There is no shared terminology to distinguish them.

### 1.5 DOC/CODE divergences

None detected. CLAUDE.md's "NEVER amend" aligns with `claude_prevent_commit_rewrite.sh`'s hard block of `--amend`.

### 1.6 Recommended canonical term

**"agent branch commit"** for commits made by the coding agent on its work branch; **"commit constraint hook"** for the PreToolUse enforcement mechanism.

---

## 2. branches

### 2.1 Canonical definition

A git branch used by a background agent. The system distinguishes a well-defined naming convention:

```python
# libs/mngr/imbue/mngr/primitives.py:334
DEFAULT_BRANCH_PREFIX: Final[str] = "mngr/"

def default_branch_name(agent_name: "AgentName", prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    """Build the default branch name for an agent."""
    return f"{prefix}{agent_name}"
```

So the canonical branch for agent `"myagent"` is `mngr/myagent`.

### 2.2 All usages

- `libs/mngr/imbue/mngr/primitives.py:334–338`: `DEFAULT_BRANCH_PREFIX = "mngr/"`, `default_branch_name()`.
- `libs/mngr/imbue/mngr/primitives.py`: `DiscoveredAgent.created_branch_name: str | None` — field on a discovered agent struct; None when no branch was created during provisioning.
- `libs/mngr/imbue/mngr/utils/git_utils.py`: `get_current_branch(path, cg)` returns the current branch name; `delete_git_branch(branch_name, source_repo_path, cg)` deletes by name.
- `libs/mngr/imbue/mngr/primitives.py`: `TransferMode.GIT_WORKTREE` and `TransferMode.GIT_MIRROR` — transfer modes that depend on branches. Worktree mode creates a worktree tracking the branch; mirror mode copies all branch refs via `GIT_MIRROR_PUSH_REFSPECS`.
- `libs/mngr/imbue/mngr/utils/git_utils.py`: `GIT_MIRROR_PUSH_REFSPECS = ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]` — mirror pushes all branches.
- `.external_worktrees/forever-claude-template/scripts/git_hooks/post-commit`: skips auto-push for detached HEAD and `mindsbackup/*` branches (line pattern `"mindsbackup/"` excluded).
- `libs/mngr/imbue/mngr/api/git.py`: `git_push(local_path, remote_host, remote_path, extra_args, cg)` and `git_pull(...)` take pass-through `extra_args`; a branch/refspec is supplied through `extra_args` (defaulting to current-branch-to-current-branch), not as a dedicated parameter.
- `libs/mngr/imbue/mngr/utils/git_utils.py`: `parse_worktree_git_file()`, `find_source_repo_of_worktree()` — for worktree-mode branches where the agent workdir is a git worktree pointing at a branch in the source repo.

### 2.3 Competing definitions

- **`mngr/` prefix** can also appear as a *developer* branch prefix in the monorepo itself (e.g., PR branch `gabriel/taxonomizing` on the outer repo; the `mngr/` prefix there is just naming convention adopted by developers, not machine-enforced).
- The term "branch" is also used for `mindsbackup/*` — backups of minds app state, unrelated to agent work.

### 2.4 Terminology variants

- "agent branch" — informal term used in docs
- "work branch" — informal
- "created_branch_name" — the struct field name
- "default branch" — used generically; `default_branch_name()` generates it

### 2.5 Ambiguities

- Whether every agent *must* have a branch depends on `TransferMode`. With `TransferMode.NONE` or `TransferMode.RSYNC`, no branch is necessarily created; `DiscoveredAgent.created_branch_name` may be `None`.
- The `mngr/` prefix is a default, not a requirement. `default_branch_name()` accepts a `prefix` override.

### 2.6 DOC/CODE divergences

None significant. The code is the ground truth; documentation references `mngr/<agent_name>` pattern correctly.

### 2.7 Recommended canonical term

**"agent branch"** for the `mngr/<agent_name>` branch. **`DEFAULT_BRANCH_PREFIX`** is the correct symbol name when referring to the prefix value.

---

## 3. tags / versions

### 3.1 Canonical definition

Two distinct uses of "tag" / "version" exist that must not be conflated:

**A — git tags on agent branches**: ordinary git tags transferred in mirror mode via `GIT_MIRROR_PUSH_REFSPECS` (`+refs/tags/*:refs/tags/*`).

**B — Claude Code version pinning**: the `version` field on `ClaudeAgentConfig` that specifies an exact Claude Code CLI version to use at provisioning time.

### 3.2 All usages

**Git tags:**
- `libs/mngr/imbue/mngr/utils/git_utils.py`: `GIT_MIRROR_PUSH_REFSPECS` includes `+refs/tags/*:refs/tags/*`, meaning git mirror mode copies all tags to the agent.
- No dedicated "create tag" or "list tag" abstractions in the mngr codebase beyond what git mirror provides.

**Claude Code version:**
- `libs/mngr_claude/imbue/mngr_claude/plugin.py`: `ClaudeAgentConfig.version: str | None` — if set, provisioning verifies that the running `claude --version` matches; raises if not.
- `.external_worktrees/forever-claude-template/.mngr/settings.toml:70`: `version = "2.1.160"` — FCT pins Claude Code to a specific version.

**Skills version (skills-lock.json):**
- `.external_worktrees/forever-claude-template/skills-lock.json`: schema has `"version": 1` (lock file format version, not a git tag); each skill entry has `computedHash` acting as a content version.
- The `sourceType` for blueprint skills is `"github_repo"`, so `computedHash` tracks a commit SHA–derived hash.

### 3.3 Competing definitions

| Term | Meaning |
|---|---|
| git tag | Standard git ref; transferred via mirror refspec |
| `version` (ClaudeAgentConfig) | Claude Code CLI binary version string |
| `version` (skills-lock.json) | Lock file schema version (integer) |
| `computedHash` (skills-lock.json) | Content hash of a specific skill revision |

### 3.4 Ambiguities

- No code in the repo creates or manages git tags explicitly beyond the mirror refspec. It is unclear whether agents are expected to create git tags as part of their workflow or whether the refspec is just belt-and-suspenders.

### 3.5 DOC/CODE divergences

None detected in the scope examined.

### 3.6 Recommended canonical term

Use **"git tag"** for the VCS concept, **"Claude Code version pin"** for `ClaudeAgentConfig.version`, and **"lock file version"** / **"skill content hash"** for the skills-lock.json fields.

---

## 4. remotes

### 4.1 Canonical definition

A git remote as referenced by mngr. Three distinct remote-like concepts exist:

**A — git remote `origin`**: the standard remote inside an agent's working copy, pushed to by the `post-commit` hook. Built as an SSH URL by `_build_ssh_git_url()` for host-to-host transfers.

**B — parent remote / upstream template**: FCT's `parent.toml` defines a template upstream. The skill `update-self` and `submit-upstream-changes` reference this upstream. This is a GitHub HTTPS URL, not a mngr SSH remote.

**C — `mngr git push/pull` target**: the SSH remote mngr constructs dynamically to push/pull agent branches between local and host.

### 4.2 All usages

**origin / SSH remote:**
- `libs/mngr/imbue/mngr/api/git.py`: `_build_ssh_git_url(ssh_info, remote_path) -> str` builds `ssh://user@host:port<remote_path>/.git` for push/pull. The wrapper `_build_git_url_and_env()` returns the bare local path for local hosts and this SSH URL (plus a `GIT_SSH_COMMAND` env) for remote hosts. No explicit remote name "origin" is used here — the URL is passed as a positional argument directly to `git push <url>` / `git fetch <url>`.
- `libs/mngr/imbue/mngr/utils/git_utils.py`: `derive_project_name_from_path()` — tries to infer project name from `git remote get-url origin` first, then falls back to directory name.
- `.external_worktrees/forever-claude-template/scripts/git_hooks/post-commit`: runs `git push origin HEAD:$(git rev-parse --abbrev-ref HEAD)` in background when `GH_TOKEN` is set.

**parent remote / template upstream:**
- `.external_worktrees/forever-claude-template/parent.toml` (referenced in FCT README and skills): defines the "upstream" template repo URL for `update-self` / `submit-upstream-changes` skills.
- Not a git-level remote; it is a TOML config file read by skill scripts.

**mngr git remote:**
- `libs/mngr/imbue/mngr/api/git.py`: `git_push()` calls `_configure_push_destination()`, which sets `receive.denyCurrentBranch=updateInstead` on the destination repo so a push to its checked-out branch is applied to the working tree on a fast-forward. With no refspec, `git_push` defaults to `<local_current_branch>:<remote_current_branch>` (via `_default_push_refspec()`), not `HEAD:<branch>`.
- `git_push(local_path, remote_host, remote_path, extra_args, cg)` and `git_pull(...)` take a local path, the target `OnlineHostInterface`, a remote path, pass-through `extra_args`, and a concurrency group — there is no `ctx` parameter and no named `branch` parameter; flags/refspecs flow through `extra_args`.
- `libs/mngr/imbue/mngr/cli/git.py`: `mngr git push` / `mngr git pull` reject bare local paths (`_resolve_remote_endpoint()` rejects addresses with no agent and no host); the target is a host-location address `AGENT[@HOST[.PROVIDER]][:PATH]`.

**Git common dir / trust across remotes:**
- `libs/mngr/imbue/mngr/utils/git_utils.py`: `find_git_common_dir()` and `find_git_source_path()` — resolves the real `.git` dir even for worktrees, ensuring trust grants apply to all worktrees of the same repo.

### 4.3 Competing definitions

- The word "remote" is used both as a git concept (a named URL) and in "remote host" (an SSH/Docker host running an agent). These are entirely different: "remote git" vs "remote host". Both appear in `libs/mngr/imbue/mngr/api/git.py` — `RemoteGitContext` operates git commands on a remote host, not on a git remote.

### 4.4 Terminology variants

- "origin" — git remote name used inside agent containers
- "upstream" — informal name for parent template repo
- "connector URL" — `ImbueCloudProviderConfig.connector_url` — HTTP URL for the imbue cloud API, unrelated to git remotes

### 4.5 DOC/CODE divergences

- `_build_ssh_git_url(ssh_info, remote_path)` builds a URL that is passed directly to git as a positional argument, not stored as a named remote in `.git/config`. The push/pull is therefore "remoteless" from git's config perspective, even though conceptually it is a remote operation. This could confuse operators inspecting `.git/config` on an agent. Note that mngr's git push/pull now also supports host-to-host transfers (the module abstracts "run git here or on a remote host" via `GitContextInterface` / `RemoteGitContext`), so the "remote" in a push target is a remote *host*, distinct from a git remote.

### 4.6 Recommended canonical term

**"git remote"** for the VCS concept; **"remote host"** for an SSH/Docker/cloud machine; **"template upstream"** for the parent.toml-defined FCT template source. Never use "remote" alone in documentation.

---

## 5. AI providers

### 5.1 Canonical definition

An "AI provider" in the mngr/minds context is the authentication + routing layer through which a coding agent (currently Claude Code) obtains its Anthropic credentials. This is now a **typed enum**, `AIProvider`, defined in `apps/minds/imbue/minds/primitives.py:72`, with three members: `IMBUE_CLOUD`, `API_KEY`, `SUBSCRIPTION`. Its docstring: "How the workspace agent should obtain its Anthropic credentials. Decoupled from the compute provider so any combination is valid: e.g. a user can run on a local container while still using an imbue_cloud-minted LiteLLM key for inference." The selected provider is chosen at agent-creation time in the minds desktop client and drives credential injection.

### 5.2 All usages and modes

The three modes correspond one-to-one to the `AIProvider` enum members. Resolution happens in `apps/minds/imbue/minds/desktop_client/agent_creator.py` (the `match ai_provider:` block around line 1520), with the enum also threaded through `templates.py` and the desktop client `app.py` (request handlers read `ai_provider` from the submitted form, defaulting to `AIProvider.SUBSCRIPTION`).

**Mode 1 — `AIProvider.IMBUE_CLOUD` (LiteLLM virtual key, imbue_cloud managed):**
- In minds, `agent_creator.py` mints a fresh per-agent LiteLLM virtual key via `imbue_cloud_cli.create_litellm_key(...)` and injects it as the effective `ANTHROPIC_API_KEY` with a matching `ANTHROPIC_BASE_URL` (requires a selected `account_email`).
- The imbue cloud backend mints keys via `ImbueCloudClient.create_litellm_key()` (`libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/client.py`).
- `_build_patch_claude_config_command()` in `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/host.py` writes the key into `.claude.json` as `primaryApiKey`, pointing Claude Code at the LiteLLM proxy base URL.
- The local dev proxy is at `litellm_proxy/config.yaml`; production is on Modal at `apps/modal_litellm/app.py` (comment in config.yaml: "model_list MUST stay in sync with apps/modal_litellm/app.py's LITELLM_CONFIG").
- `ENABLE_CLAUDEAI_MCP_SERVERS=false` is set in FCT host_env (`.external_worktrees/forever-claude-template/.mngr/settings.toml`), which disables Claude.ai MCP server integration that would conflict with LiteLLM routing.

**Mode 2 — `AIProvider.API_KEY` (raw ANTHROPIC_API_KEY):**
- In minds, `agent_creator.py` uses the user-supplied `anthropic_api_key` directly and sets no `ANTHROPIC_BASE_URL`, so the agent talks to the official Anthropic API (raises if no key is supplied).
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/host.py` (docstring): "claude config when `ANTHROPIC_API_KEY` is set anywhere in env (the LiteLLM key flows through `--pass-host-env` for minds, so...". Raw Anthropic key bypasses LiteLLM entirely.
- `litellm_proxy/config.yaml`: all models use `api_key: os.environ/ANTHROPIC_API_KEY` at the proxy level. So even the proxy mode ultimately uses a raw key — but at the proxy layer, not per-agent.

**Mode 3 — `AIProvider.SUBSCRIPTION` (Claude.ai OAuth subscription credentials):**
- In minds, `agent_creator.py` injects neither key nor base URL; the user signs in to Claude interactively once the workspace starts.
- `libs/mngr_claude/imbue/mngr_claude/claude_config.py`: `get_user_claude_config_dir()` reads `ORIGINAL_CLAUDE_CONFIG_DIR` to locate the user's original `~/.claude/` dir with OAuth tokens (from `mngr login` / Claude Code's web-based login).
- `libs/mngr_claude/imbue/mngr_claude/resources/sync_keychain_credentials.py`: `build_credential_sync_hooks_config()` writes a `Notification:auth_success` hook to sync macOS Keychain credentials back to the user's config after OAuth refresh.
- `ClaudeAgentConfig.sync_claude_credentials: bool` — when True, copies OAuth credentials from `ORIGINAL_CLAUDE_CONFIG_DIR` to the agent's isolated config dir at provisioning time.

**mngr plugin catalog — coding-agent signal checks:**
- `libs/mngr/imbue/mngr/plugin_catalog.py`: `ClaudeSignalCheck` (checks `claude --version`), `OpenCodeSignalCheck` (checks `opencode --version`), `CodexSignalCheck` (checks `codex --version`), `AntigravitySignalCheck` (checks `agy --version`), `PiSignalCheck` (probes `pi --help`) — mngr detects which coding agent binaries are present and enables/disables corresponding plugins. Note these detect *compute/coding-agent* binaries, not the minds `AIProvider` credential mode.

### 5.3 Competing definitions / terminology variants

| Term | Where used | Meaning |
|---|---|---|
| `provider` (mngr) | `ImbueCloudProviderConfig`, `VpsDockerProviderConfig` | Compute host provider (cloud/docker/local), NOT LLM provider |
| `AIProvider` (minds) | `apps/minds/imbue/minds/primitives.py:72` | Typed enum for the credential mode: `IMBUE_CLOUD` / `API_KEY` / `SUBSCRIPTION` |
| LiteLLM | Code | Proxy that virtualizes LLM API calls |
| `primaryApiKey` | `.claude.json` | The key written into Claude Code's config — could be a LiteLLM virtual key OR a raw Anthropic key |

The word "provider" in mngr almost always means *compute* provider (who runs the machine). In minds, "AI provider" is now its own typed concept (`AIProvider`), decoupled from the compute provider — but the two senses of "provider" remain a source of confusion.

### 5.4 Ambiguities

- The credential mode is now statically typed in minds as the `AIProvider` enum (`IMBUE_CLOUD` / `API_KEY` / `SUBSCRIPTION`), selected at agent-creation time. This typing lives in the minds desktop client, not in mngr's `ClaudeAgentConfig`; mngr itself still receives the *result* (env vars like `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`) rather than the enum, so at the mngr provisioning layer the distinction remains env-determined.
- The LiteLLM proxy URL is not stored in a single canonical constant; it is assembled from the `connector_url` field and inferred config.

### 5.5 DOC/CODE divergences

- Documentation may refer to "imbue_cloud provider" meaning the compute layer. The same name `IMBUE_CLOUD` is *also* an `AIProvider` member (the LiteLLM-key mode) *and* a `LaunchMode`/`BackupProvider` member, so "imbue_cloud" now denotes at least three orthogonal axes. The enum docstrings stress these are decoupled, but the shared member name invites conflation.
- Terminology tension with the code: this taxonomy's prior guidance was to reserve "provider" strictly for compute providers and never say "AI provider." The merged code contradicts that by naming the enum `AIProvider` (and its values `IMBUE_CLOUD` / `API_KEY` / `SUBSCRIPTION`). The canonical type name in code is now `AIProvider`.

### 5.6 Recommended canonical term

**`AIProvider`** (matching the code) for the three-way credential-mode distinction; its members are **`IMBUE_CLOUD`** (LiteLLM virtual key), **`API_KEY`** (raw Anthropic API key), and **`SUBSCRIPTION`** (OAuth subscription credentials). Reserve bare **"provider"** for compute host providers, and qualify as **"AI provider"** vs **"compute provider"** when both are in scope. Canonical location: `apps/minds/imbue/minds/primitives.py:72`.

---

## 6. models

### 6.1 Canonical definition

A model in this system is the LLM served to a coding agent. The system has two levels: the *model alias* that the agent requests (e.g., `"opus[1m]"`), and the *concrete model version* that the LiteLLM proxy routes to (e.g., `claude-opus-4-8`).

### 6.2 All usages

**FCT model selection (settings layer):**
- `.external_worktrees/forever-claude-template/.claude/settings.json`: `"model": "opus[1m]"`, `"effortLevel": "high"` — top-level user-facing model alias.
- `.external_worktrees/forever-claude-template/.mngr/settings.toml:70`: `settings_overrides__extend = {model = "opus[1m]", fastMode = false}` for the `claude` agent type.

**Model aliases:**
- `opus[1m]` — Opus-class model with 1M context window. The `[1m]` suffix is a Claude Code convention for context-window selection; it is not a standard Anthropic API model name.
- The alias is resolved by the LiteLLM proxy or Claude Code internally to a concrete model ID.

**LiteLLM proxy model list (`litellm_proxy/config.yaml`):**
- Opus tier: `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`, `claude-opus-4-5`, `claude-opus-4-1`, `claude-opus-4-20250514`
- Sonnet tier: `claude-sonnet-4-6`, `claude-sonnet-4-5`, `claude-sonnet-4-20250514`
- Haiku tier: `claude-haiku-4-5`, `claude-haiku-4-5-20251001`
- All entries use `api_key: os.environ/ANTHROPIC_API_KEY` (proxy-level raw key)
- Comment: "model_list MUST stay in sync with apps/modal_litellm/app.py's LITELLM_CONFIG"

**Fast/slow tiers:**
- `.external_worktrees/forever-claude-template/.mngr/settings.toml`: `fastMode = false` explicitly disabled for worker agents. Claude Code's `fastMode` setting selects a faster, cheaper model tier (Haiku-class) for internal scaffolding calls.
- `_UNATTENDED_SETTINGS_FLAGS` in `libs/mngr_claude/imbue/mngr_claude/plugin.py`: `{"skipDangerousModePermissionPrompt": True, "fastMode": False}` — `fastMode` defaults to False in unattended mode.

**Antigravity (Google Gemini):**
- `libs/mngr_antigravity/imbue/mngr_antigravity/antigravity_config.py`: `agy` (Antigravity) agent uses Google models. No model alias appears in the examined code; configuration is via `$HOME/.gemini/config/`.

### 6.3 Competing definitions / terminology variants

| Term | Meaning |
|---|---|
| `model` (settings.json / settings.toml) | Claude Code alias string (e.g. `"opus[1m]"`) |
| model (litellm config) | Concrete Anthropic model ID (e.g. `"claude-opus-4-8"`) |
| `fastMode` | Boolean flag selecting fast/cheap model tier in Claude Code's scaffolding |
| `effortLevel` | Claude Code setting (`"high"`) affecting response depth, distinct from model selection |

### 6.4 Ambiguities

- The `[1m]` suffix syntax is undocumented in this codebase (it is a Claude Code feature). Its resolution is opaque from mngr's perspective.
- No "model registry" abstraction exists in mngr; the LiteLLM config YAML *is* the model registry, and it must be kept in sync manually.
- There is no per-task model selection in the examined code — the model is set globally per agent.

### 6.5 DOC/CODE divergences

- The config comment ("model_list MUST stay in sync") indicates a manual synchronization requirement with no automated enforcement. This is a known operational risk.
- `fastMode = false` appears in both FCT settings.toml and mngr's `_UNATTENDED_SETTINGS_FLAGS`, but there is no code assertion that they agree.

### 6.6 Recommended canonical term

**"model alias"** for the Claude Code-level string (e.g., `"opus[1m]"`); **"concrete model ID"** for the Anthropic API model name (e.g., `"claude-opus-4-8"`); **"model tier"** for Opus/Sonnet/Haiku classifications. Avoid using "model" alone without qualification.

---

## 7. tools / MCP servers

### 7.1 Canonical definition

Two distinct "tool" concepts exist and must not be conflated:

**A — Claude Code tools**: built-in capabilities the Claude Code agent can invoke (Read, Edit, Bash, WebSearch, etc.). These are the tools the agent uses to do work.

**B — MCP servers**: external processes Claude Code connects to that expose additional tools via the Model Context Protocol. These expand the agent's toolset beyond built-ins.

### 7.2 All usages

**Disallowed tools (FCT):**
- `.external_worktrees/forever-claude-template/.mngr/settings.toml`: `cli_args = "--dangerously-skip-permissions --disallowed-tools AskUserQuestion,ExitPlanMode,TodoWrite,TaskCreate,TaskList,TaskUpdate"` — six built-in Claude Code tools are explicitly disabled for worker agents. Note: `TodoWrite`, `TaskCreate`, `TaskList`, `TaskUpdate` are disabled; but `TaskStop`, `TaskOutput`, `TaskGet` are not mentioned (unclear if also disabled).

**Tools skipped by hooks:**
- `.external_worktrees/forever-claude-template/scripts/claude_require_steps_pretool.sh`: lists read-only / meta tools that are exempt from step-tracking requirements: Read, Glob, Grep, WebFetch, WebSearch, ToolSearch, Skill, TaskCreate, TaskUpdate, TaskGet, TaskList, TaskOutput, TaskStop, LSP, Monitor, SendMessage, EnterPlanMode, ExitPlanMode, `mcp__sculptor__*`.

**MCP servers:**
- `ENABLE_CLAUDEAI_MCP_SERVERS=false` in FCT host_env (`.external_worktrees/forever-claude-template/.mngr/settings.toml`): disables Claude.ai's built-in MCP server integration.
- `.external_worktrees/forever-claude-template/.claude/settings.json`: `extraKnownMarketplaces` lists `imbue-code-guardian` and `claude-code-plugins`. The code-guardian plugin may expose MCP tools.
- `mcp__sculptor__*` tool prefix seen in `claude_require_steps_pretool.sh` — indicates the Sculptor MCP server exposes tools like `mcp__sculptor__ask_user_question`, `mcp__sculptor__exit_plan_mode`.
- `mcp__plugin_playwright_playwright__*` tools visible in the current session's deferred tool list — Playwright MCP server for browser automation.

**Tool naming convention:**
- MCP tools are named `mcp__<server_name>__<tool_name>` (double underscores). The server name corresponds to the MCP server registration name.

### 7.3 Competing definitions

The word "tool" is used for:
- Claude Code built-in tools (Read, Edit, Bash…)
- mngr CLI subcommands (informal: "the mngr tool")
- MCP-exposed functions (`mcp__sculptor__*`)

### 7.4 Ambiguities

- The boundary between "Claude Code plugin" and "MCP server" is blurry: a Claude Code plugin (npm package) can *provide* MCP servers. The `imbue-code-guardian` plugin is listed as both a `plugin` in `enabledPlugins` and implicitly provides MCP tools.
- `ENABLE_CLAUDEAI_MCP_SERVERS=false` disables Claude.ai's *built-in* MCP integration. Third-party MCPs configured via `settings.json` are not affected by this flag.

### 7.5 DOC/CODE divergences

- The disallowed tools list in FCT settings.toml excludes `TodoWrite` but not `TodoRead` — this may be intentional (reading todo list is OK) but is undocumented.
- `claude_require_steps_pretool.sh` exempts `TaskCreate` from step checks even though FCT also *disables* `TaskCreate` via `--disallowed-tools`. This inconsistency is harmless (the hook guard is moot if the tool is disabled) but reflects uncoordinated maintenance.

### 7.6 Recommended canonical term

**"Claude Code tool"** for built-in agent capabilities; **"MCP server"** for external process providing tools via Model Context Protocol; **"MCP tool"** for a function exposed by an MCP server. The `mcp__<server>__<name>` naming convention is the ground-truth identifier for MCP tools.

---

## 8. skills

### 8.1 Canonical definition

A Claude Code skill is a markdown file (`.md`) in `.agents/skills/` that Claude Code loads as a `/` slash command. When the user types `/<skill_name>`, Claude Code reads the markdown file as a prompt and executes its instructions. Skills are distinct from Claude Code plugins (npm packages).

### 8.2 All usages

**Skill storage and discovery:**
- `.agents/skills/` directory inside the agent's project directory contains installed skills.
- FCT has 18 skills (from `/tmp/fct_skills.txt`): `blueprint`, `blueprint-generate`, `build-web-service`, `crystallize-task`, `dealing-with-the-unexpected`, `do-something-new`, `edit-services`, `file-sharing`, `heal-skill`, `latchkey`, `launch-task`, `manage-layout`, `read-telegram-history`, `send-telegram-message`, `send-user-message`, `submit-upstream-changes`, `update-self`, `update-skill`, `welcome`.

**Skills lock (`skills-lock.json`):**
- Root `skills-lock.json` and `.external_worktrees/forever-claude-template/skills-lock.json` contain identical content.
- Schema (version 1):
  ```json
  {
    "version": 1,
    "skills": {
      "<name>": {
        "source": "https://github.com/<org>/<repo>",
        "sourceType": "github_repo",
        "skillPath": "<path/in/repo>",
        "computedHash": "<hex>"
      }
    }
  }
  ```
- Only `blueprint` and `blueprint-generate` are in the lock file; the other 16 FCT skills are NOT in `skills-lock.json` — they come from the FCT template repo itself (bundled, not fetched via lock).

**Worker sub-skills:**
- `.external_worktrees/forever-claude-template/.agents/shared/scripts/install_worker_skills.sh`: at crystallize-worker provision time, installs "worker sub-skills" from `assets/worker/` subdirectories of parent skills into the worker's `.agents/skills/`.
- This is a two-level skill hierarchy: parent skills (on main agent) contain `assets/worker/` dirs with child skills installed into worker agents.

**Home-dir sync:**
- `libs/mngr_claude/imbue/mngr_claude/plugin.py`: `_CLAUDE_HOME_SYNC_DIRS = ("skills", "agents", "commands", "plugins")` — skills are one of four directories synced from user's Claude config to the agent config dir during provisioning (when `sync_home_settings = True`).
- Note: `agents` here means Claude Code's `.claude/agents/` (sub-agents), not mngr agents.

**`update-skill` skill:**
- FCT has an `update-skill` skill for updating/healing skills in place. The `heal-skill` skill is also present, suggesting skills can become inconsistent with their upstream sources.

### 8.3 Competing definitions / terminology variants

| Term | Meaning |
|---|---|
| skill (Claude Code) | Markdown prompt file in `.agents/skills/` invokable as `/name` |
| skills-lock.json | Version-pinned registry of externally sourced skills |
| worker sub-skill | Child skill installed into crystallize-worker's `.agents/skills/` from parent's `assets/worker/` |
| `agents` (sync dir) | Claude Code sub-agents config (`.claude/agents/`), NOT mngr agents |
| `commands` (sync dir) | Claude Code custom commands (`.claude/commands/`), also markdown files |

### 8.4 Ambiguities

- The distinction between "skill" (`.agents/skills/`) and "command" (`.claude/commands/`) is not clearly explained anywhere in the examined code. Both appear to be markdown invokable as slash commands; the structural difference is unclear from code alone.
- Only 2 of 18 FCT skills are in `skills-lock.json`. The other 16 are "free" — their provenance and update mechanism is the `update-skill` and `heal-skill` skills themselves, creating a bootstrapping dependency.

### 8.5 DOC/CODE divergences

- `libs/mngr_claude/imbue/mngr_claude/plugin.py` syncs `.claude/agents/` under the key `"agents"` in `_CLAUDE_HOME_SYNC_DIRS`. This "agents" is Claude Code's sub-agent definitions directory, NOT mngr's agent concept. Using "agents" as a sync dir name in the mngr codebase is a terminology collision.

### 8.6 Recommended canonical term

**"skill"** for a markdown slash-command file in `.agents/skills/`; **"skills lock"** for `skills-lock.json`; **"worker skill"** for child skills in `assets/worker/`. To disambiguate from Claude Code sub-agents, say **"Claude Code sub-agent definitions"** not "agents" when referring to `.claude/agents/`.

---

## 9. hooks

### 9.1 Canonical definition

The word "hook" in this codebase refers to three completely distinct systems that must not be conflated:

**System A — Claude Code event hooks**: Shell commands configured in `.claude/settings.json` / `settings.local.json` that fire on Claude Code lifecycle events. Configured by mngr at provisioning time and/or by FCT's own `settings.json`.

**System B — mngr pluggy lifecycle hooks**: Python functions registered via pluggy's `hookspec`/`hookimpl` mechanism for extending mngr's own lifecycle. Defined in `libs/mngr/imbue/mngr/plugins/hookspecs.py`.

**System C — git hooks**: Shell scripts executed by git at specific git workflow points. In FCT, only one git hook exists: `post-commit`.

### 9.2 All usages

**System A — Claude Code event hooks:**

Events (from `libs/mngr_claude/imbue/mngr_claude/claude_config.py` and FCT `settings.json`):
- `SessionStart`: runs at session start. FCT: `uv sync --all-packages`, `claude_update_plugin.sh`, `ensure_tk_on_path.sh`.
- `PreToolUse`: runs before each tool invocation. FCT: `claude_prevent_commit_rewrite.sh` (hard-blocks `--amend`/rebase), `claude_tk_standalone.sh`, `claude_require_steps_pretool.sh` (non-blocking reminder), `claude_tk_close_reoutput_nudge.sh`.
- `UserPromptSubmit`: runs when user submits a prompt. FCT: `claude_open_tickets_reminder.sh`.
- `PermissionRequest`: runs when Claude Code requests permission. FCT (mngr-provisioned): wildcard auto-allow via `build_permission_auto_allow_hooks_config()`.
- `PostToolUse`: runs after tool completes (success). Configured by mngr via `build_readiness_hooks_config()`.
- `PostToolUseFailure`: runs after tool fails. Configured by mngr.
- `Notification`: runs on notification events. FCT: none. mngr-provisioned: `auth_success` → macOS Keychain sync (`build_credential_sync_hooks_config()`).
- `Stop`: runs when agent session ends. FCT: git presence check, `claude_open_tickets_stop_nudge.sh`. mngr-provisioned: readiness tracking.

Exit code semantics (Claude Code):
- Exit 0: allow / no output
- Exit 2: **hard block** (tool is rejected, never executes). Used by `claude_prevent_commit_rewrite.sh`.
- Other non-zero: soft warning (Claude Code sees the output but continues)

`SESSION_GUARD` in `libs/mngr_claude/imbue/mngr_claude/claude_config.py`: `'[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0; '` — prefix prepended to mngr-provisioned hooks so they only fire in the "main" Claude session, not in sub-sessions.

**Status line** (a hook-adjacent feature):
- `.external_worktrees/forever-claude-template/.claude/settings.json`: `"statusLine": {"type": "command", "command": "...scripts/claude_status_line.sh"}` — a shell command polled to populate Claude Code's UI status bar. Outputs `[HH:MM:SS user@host dir] branch | PR: url (status)`.

**System B — mngr pluggy lifecycle hooks (`hookspecs.py`):**

```python
# libs/mngr/imbue/mngr/plugins/hookspecs.py:28
hookspec = pluggy.HookspecMarker("mngr")
```

Full list of hookspecs: `register_provider_backend`, `register_agent_type`, `register_agent_aliases`, `on_before_host_create`, `on_host_created`, `on_before_host_destroy`, `on_host_destroyed`, `on_before_initial_file_copy`, `on_after_initial_file_copy`, `on_agent_state_dir_created`, `on_before_provisioning`, `on_after_provisioning`, `on_agent_created`, `on_before_agent_destroy`, `on_agent_destroyed`, `register_cli_options`, `on_load_config`, `register_cli_commands`, `register_help_topics`, `override_command_options`, `get_files_for_deploy`, `modify_env_vars_for_deploy`, `agent_field_generators`, `offline_agent_field_generators`, `on_before_create`, `on_post_install`, `on_startup`, `on_before_command`, `on_after_command`, `on_error`, `on_shutdown`, `register_hookspecs`. (`register_agent_aliases` is the newer hookspec that maps short alias names like `agy`/`pi` to canonical agent types.)

These are Python-level hooks in mngr's plugin system, completely separate from Claude Code event hooks.

**`LifecycleHook` enum (`libs/mngr/imbue/mngr/primitives.py`):**
- `INITIALIZE`, `ON_CREATE`, `UPDATE_CONTENT`, `POST_CREATE`, `POST_START`, `POST_ATTACH` — mngr's agent lifecycle stages. This is a mngr concept, NOT a Claude Code event.

**System C — git hooks:**
- `.external_worktrees/forever-claude-template/scripts/git_hooks/post-commit`: the only git hook in FCT.
- Registered globally via `git config --global core.hooksPath /mngr/code/scripts/git_hooks`.
- Auto-pushes current branch to `origin` in background when `GH_TOKEN` is set; skips detached HEAD and `mindsbackup/*` branches.
- The directory `/mngr/code/scripts/git_hooks/` is a global hooksPath, meaning it applies to all git operations on the container, not just one repo.

**Antigravity hooks:**
- `libs/mngr_antigravity/imbue/mngr_antigravity/antigravity_config.py`: `build_antigravity_hooks_config()` writes `$HOME/.gemini/config/hooks.json` with a single `PreInvocation` handler (running `capture_conversation_id.sh` to record the conversation IDs this agent touches, for transcript scoping). The agent's RUNNING/WAITING lifecycle is no longer hook-driven — it is driven by the mngr-owned `statusLine` command (`build_antigravity_statusline_settings`), and permission auto-approval goes through the per-agent `settings.json` permissions block / `--dangerously-skip-permissions` rather than a hook.

### 9.3 Competing definitions summary

| Term | System | Config location |
|---|---|---|
| Claude Code event hook | Shell command on lifecycle events | `.claude/settings.json`, `.claude/settings.local.json` |
| mngr pluggy hook | Python `hookimpl` function | `hookspecs.py`, plugin modules |
| `LifecycleHook` enum | mngr agent lifecycle stage | `primitives.py` |
| git hook | Shell script at git workflow points | `scripts/git_hooks/post-commit` |
| antigravity hook | Shell command on agy lifecycle events | `$HOME/.gemini/config/hooks.json` |

### 9.4 Ambiguities

- The `LifecycleHook` enum values (`POST_CREATE`, `POST_START`, etc.) sound similar to Claude Code hook event names (`Stop`, `SessionStart`) but are entirely different systems.
- mngr uses `on_before_provisioning` / `on_after_provisioning` pluggy hooks to inject logic around agent setup; this is not the same as Claude Code's `SessionStart`. They fire in different processes (mngr host process vs. agent Claude Code process).

### 9.5 DOC/CODE divergences

- The Claude Code hook documentation (upstream Anthropic docs) lists hook events. The mngr codebase in `build_readiness_hooks_config()` uses `PostToolUse` and `PostToolUseFailure` as event names — these are confirmed present in Claude Code's settings.json schema.
- One potential divergence: `claude_require_steps_pretool.sh` exempts `TaskCreate`, `TaskUpdate`, etc. from step checks — but FCT's `--disallowed-tools` also disables `TaskCreate`, `TaskList`, `TaskUpdate`. The hook exemption for disabled tools is moot but represents a maintenance consistency issue.

### 9.6 Recommended canonical term

Use **"Claude Code hook"** for System A (event-driven shell commands); **"mngr plugin hook"** or **"pluggy hook"** for System B (Python hookspecs); **"git hook"** for System C. The `LifecycleHook` enum should be called **"mngr lifecycle stage"** to avoid collision with hook terminology.

---

## 10. plugins

### 10.1 Canonical definition

Two completely distinct plugin systems coexist and are both called "plugins":

**System A — mngr pluggy plugins**: Python packages implementing mngr's pluggy hookspec interface. They extend mngr's CLI, agent types, provider backends, and lifecycle. Managed by `libs/mngr/imbue/mngr/plugin_catalog.py` and `libs/mngr/imbue/mngr/config/plugin_registry.py`.

**System B — Claude Code plugins**: npm/Node.js packages installed into Claude Code via its `/plugin` command. They extend Claude Code's capabilities (MCP servers, marketplace integrations). Tracked in `plugins/installed_plugins.json` and `plugins/known_marketplaces.json` within the Claude config dir.

### 10.2 All usages

**System A — mngr pluggy plugins:**

```python
# libs/mngr/imbue/mngr/plugins/hookspecs.py:28
hookspec = pluggy.HookspecMarker("mngr")
```

- `libs/mngr/imbue/mngr/plugin_catalog.py`: `PLUGIN_CATALOG` — tuple of `CatalogEntry` objects for all known mngr plugins (each keyed by `entry_point_name`). Entries: `claude`, `opencode`, `codex`, `antigravity`, `pi_coding`, `modal`, `lima`, `vultr`, `aws`, `gcp`, `ovh`, `tutor`, `code_guardian`, `fixme_fairy`, `headless_claude`, `ttyd`, `file`, `kanpan`, `notifications`, `pair`, `recursive`, `schedule`, `tmr`, `wait`.
- `libs/mngr/imbue/mngr/config/plugin_registry.py`: `_plugin_config_registry: dict[PluginName, type[PluginConfig]]` — runtime registry mapping plugin name → config class. `register_plugin_config()`, `get_plugin_config_class()`, `list_registered_plugins()`.
- Each mngr plugin is a separate Python package under `libs/mngr_<name>/`. The plugin is activated when its package is installed and its `hookimpl`-decorated functions are discovered by pluggy.
- Signal checks: `ClaudeSignalCheck` (`claude --version`), `OpenCodeSignalCheck` (`opencode --version`), `CodexSignalCheck` (`codex --version`), `AntigravitySignalCheck` (`agy --version`), `PiSignalCheck` (probes `pi --help`), plus `ModalSignalCheck` / `LimaSignalCheck` for compute plugins — mngr auto-detects which binaries/credentials are present and enables/disables plugins accordingly.

**Agent types and aliases:**
- mngr plugins register agent types via the `register_agent_type` pluggy hookspec (`libs/mngr/imbue/mngr/plugins/hookspecs.py`), returning `(agent_type_name, agent_class, config_class)`. The agent-type registry records which plugin owns each type via `register_agent_type_owner()` in `libs/mngr/imbue/mngr/config/agent_plugin_registry.py` (invoked internally by `agents/agent_registry.py`). `codex`, `opencode`, `pi_coding`, and `antigravity` are now real agent-type implementations rather than stubs.
- A separate `register_agent_aliases` hookspec lets a plugin expose short alternate names that resolve to a canonical agent type before any registry lookup. The antigravity plugin maps `agy` → `antigravity` (`libs/mngr_antigravity/.../plugin.py`), and the pi_coding plugin maps `pi` → `pi-coding` (`libs/mngr_pi_coding/.../plugin.py`). An alias is a name-resolution entry only (never a distinct type); `resolve_agent_type()` in `libs/mngr/imbue/mngr/config/agent_config_registry.py` resolves an alias to its canonical type, which then shares that type's class, config, and disabled-plugin handling. Aliases that collide with an existing type or alias, or point at an unregistered target, are skipped.
- `UNPUBLISHED_PACKAGES: frozenset` — set of packages not on PyPI; these require local path installation.
- FCT `disable_plugin__extend = ["recursive", "ttyd", "claude_subagent_proxy"]` (`.external_worktrees/forever-claude-template/.mngr/settings.toml`) — disables specific mngr plugins for FCT deployments. Note `claude_subagent_proxy` is now **disabled by default** in mngr itself (it only loads when a config layer sets `[plugins.claude_subagent_proxy] enabled = true`), because it is experimental and intercepts Claude Code's built-in `Task` tool; the FCT entry is therefore belt-and-suspenders.

**System B — Claude Code plugins:**

- `libs/mngr_claude/imbue/mngr_claude/plugin.py`:
  - `_INSTALLED_PLUGINS_RELATIVE_PATH = Path("plugins") / "installed_plugins.json"` — JSON file tracking installed Claude Code plugins.
  - `_KNOWN_MARKETPLACES_RELATIVE_PATH = Path("plugins") / "known_marketplaces.json"` — JSON file tracking known plugin marketplaces.
  - `_INSTALLED_PLUGINS_SENTINEL_PREFIX = "/__mngr_plugins_source__"` — prefix in `installed_plugins.json` paths that gets rewritten at deploy time to actual filesystem paths.
  - `_CLAUDE_HOME_SYNC_DIRS = ("skills", "agents", "commands", "plugins")` — "plugins" here is the Claude Code plugins directory, synced from user's Claude config to the agent's config dir.
- `.external_worktrees/forever-claude-template/.claude/settings.json`:
  - `extraKnownMarketplaces`: `{"imbue-code-guardian": {"url": "...", "name": "imbue-code-guardian"}, "claude-code-plugins": {"url": "...", "name": "claude-code-plugins"}}` — registers additional marketplace sources with Claude Code.
  - `enabledPlugins`: `{"imbue-code-guardian@imbue-code-guardian": true, "frontend-design@claude-code-plugins": true}` — specific plugins enabled from those marketplaces.

**Plugin name format for Claude Code:**
- Format: `"<plugin-name>@<marketplace-name>"` — e.g., `"imbue-code-guardian@imbue-code-guardian"`.

**CLAUDE_CONFIG_DIR:**
- `libs/mngr_claude/imbue/mngr_claude/claude_config.py`: `get_claude_config_dir()` reads `$CLAUDE_CONFIG_DIR` or defaults to `~/.claude/`. Per-agent isolated config dirs live at `<agent_state_dir>/plugin/claude/anthropic/`.
- `ORIGINAL_CLAUDE_CONFIG_DIR`: env var preserving the user's original Claude config dir, allowing credential sync between user context and agent context.
- `use_env_config_dir = true` in FCT settings.toml — agents share a single `CLAUDE_CONFIG_DIR` from the environment rather than each having an isolated copy.

### 10.3 Competing definitions summary

| Term | System | Language | Registry |
|---|---|---|---|
| mngr plugin | Pluggy-based Python extension | Python | `plugin_catalog.py`, `plugin_registry.py` |
| Claude Code plugin | npm/Node.js Claude Code extension | JavaScript/TypeScript | `installed_plugins.json`, `known_marketplaces.json` |
| marketplace | Claude Code plugin source registry | — | `extraKnownMarketplaces` in settings.json |

### 10.4 Ambiguities

- The word "plugin" without qualification is deeply ambiguous in any context involving both mngr and Claude Code. For example, `disable_plugin__extend = ["recursive", "ttyd"]` in FCT settings.toml refers to *mngr* plugins, but `enabledPlugins` in FCT settings.json refers to *Claude Code* plugins. Same word, adjacent files, opposite systems.
- The `plugins` directory in `_CLAUDE_HOME_SYNC_DIRS` refers to Claude Code's plugin directory, but `libs/mngr_claude/` is itself a mngr plugin. Two uses of "plugin" in a single import chain.

### 10.5 DOC/CODE divergences

- The `_INSTALLED_PLUGINS_SENTINEL_PREFIX = "/__mngr_plugins_source__"` mechanism is undocumented — it rewrites plugin paths at deploy time but there is no in-code docstring explaining the rewrite lifecycle. This is a potential source of confusion for anyone debugging plugin installation failures.
- Claude Code's `extraKnownMarketplaces` and `enabledPlugins` are configured in FCT's `settings.json` (static file), but mngr's provisioning in `plugin.py` also writes `settings.local.json`. The interaction between the two settings files (which takes precedence for plugin config?) is not documented.

### 10.6 Recommended canonical term

**"mngr plugin"** for Python pluggy extensions; **"Claude Code plugin"** for npm/Node.js Claude Code extensions; **"plugin marketplace"** for Claude Code's `extraKnownMarketplaces`. Never use "plugin" alone in any cross-system context. Introduce a consistent qualifier in all documentation and code comments.

---

## Cross-cutting inconsistencies (headline findings)

- **"provider" means two things**: In mngr, `provider` always means *compute host provider* (cloud/docker/local). The credential/routing axis is now a separate typed concept in minds: the `AIProvider` enum (`apps/minds/imbue/minds/primitives.py:72`) with members `IMBUE_CLOUD` / `API_KEY` / `SUBSCRIPTION`, explicitly decoupled from the compute provider. The `IMBUE_CLOUD` member name is shared across `AIProvider`, `LaunchMode`, and `BackupProvider`, so the *value* name still invites conflation even though the types are distinct.

- **"plugin" is maximally ambiguous**: `disable_plugin__extend` (mngr pluggy) and `enabledPlugins` (Claude Code npm) are both called "plugins", both appear in FCT config, and both affect what a Claude agent can do — but they are entirely different systems with different registries, languages, and install mechanisms.

- **"hook" has four meanings**: Claude Code event hook (shell), mngr pluggy hook (Python), `LifecycleHook` enum (mngr stage name), and git hook (git shell script). The `LifecycleHook` enum naming is the worst offender — it sounds like it might relate to Claude Code's hook system but is actually a mngr provisioning concept.

- **"agents" directory collision**: `_CLAUDE_HOME_SYNC_DIRS` includes `"agents"` to sync Claude Code's `.claude/agents/` (sub-agent definitions). In the mngr codebase, "agents" means something entirely different (mngr-managed background agents). The collision is in the same Python file as most of the mngr-agent lifecycle code.

- **Model registry is not typed**: There is no `ModelRegistry` type or enum in the codebase. The LiteLLM proxy config YAML is the de facto model registry, maintained manually with an explicit comment warning about sync requirements. No automated consistency check exists.

- **Skills lock only covers 2 of 18 FCT skills**: The other 16 FCT skills are bundled in the template repo with no cryptographic pinning. Their update mechanism is itself a skill (`update-skill`), creating a bootstrapping risk.

- **`remote` means host OR git remote**: `RemoteGitContext` runs git commands *on a remote host* (SSH), not on a git remote. But `git push origin` pushes to a git remote called `origin`. The same word is used for a compute location and a VCS endpoint throughout `libs/mngr/imbue/mngr/api/git.py`.
