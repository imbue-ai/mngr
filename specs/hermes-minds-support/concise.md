# Hermes Agent Type Support for Minds

## Overview

* The minds system currently assumes Claude Code as the agent runtime. This adds hermes as an alternative agent type, running inside the same forever-claude-template repo with minimal agent-specific divergence.
* The template repo gains a thin agent-specific layer resolved at provisioning time: a setup script copies/generates the right declaration files (instruction filename, hook wiring, config) based on agent type. The agent only sees its own config -- hermes agents never see `.claude/` files and vice versa.
* Shared content is maximized: one `AGENTS.md` instructions file (copied to `CLAUDE.md` for claude agents), one set of skills in `.agents/skills/`, shared shell scripts for hook behaviors. Only the *declarations* that wire these into each runtime differ.
* Implementation is template-only initially (no mngr_hermes plugin changes). Hermes config is placed as static files + a provisioning script in the template, with the design allowing promotion to declarative plugin config later.
* The minds desktop UI gains an "Agent type" dropdown so users can select hermes or claude at creation time. The existing minds-dev-iterate workflow is compatible -- the user just selects the agent type in the UI.

## Expected Behavior

* **Creating a hermes mind**: User selects "hermes" in the minds desktop UI agent type dropdown, picks a launch mode (dev/local/docker), and clicks create. The agent starts `hermes chat` in tmux with an isolated HERMES_HOME containing the template's config (model, toolsets, skills, hooks).
* **Shared instructions**: Both agent types read the same operational instructions (communication via telegram, work delegation, service management, self-modification, etc.). Claude agents see it as `CLAUDE.md`, hermes agents see it as `AGENTS.md`. The canonical source in the template is `AGENTS.md`.
* **Shared skills**: Skills in `.agents/skills/` (send-telegram-message, launch-task, edit-services, etc.) are available to both agent types. Claude discovers them via `.claude/skills/` symlink. Hermes discovers them via `skills.external_dirs` in config.yaml pointing to the work directory's `.agents/skills/`.
* **Shared hook behaviors**: Both agents get the same runtime behaviors (setup on start, guard rails on tool use, repo root check on stop). Claude wires these via `.claude/settings.json` hooks calling shared scripts. Hermes wires these via Python plugin files in `HERMES_HOME/plugins/` that call the same shared scripts.
* **Hermes model default**: `anthropic/claude-opus-4.6` configured in the template's hermes config.yaml, merged on top of the user's `~/.hermes/config.yaml` if it exists (preserving user's provider endpoints, API settings, etc.). If `~/.hermes` doesn't exist, hermes defaults are used as the base.
* **Dockerfile**: Always installs Claude Code CLI (existing). Conditionally installs hermes-agent when a hermes agent type is selected. The mngr_hermes plugin is registered alongside mngr_claude.
* **Worker agents**: The `[agent_types.worker]` continues to inherit from claude. Hermes is the "brain" (main agent); claude handles coding tasks via delegation. The launch-task skill works unchanged. This can be revisited later to support hermes workers.
* **Event processors**: Deferred for hermes minds initially. The persistent agent pattern depends on Claude Code's stop hook (exit code 2). Hermes gateway mode or mngr restart could serve this purpose later -- the design doesn't preclude either.
* **minds-dev-iterate**: Works unchanged. The dev template's `extra_provision_command` is extended to install mngr_hermes when hermes is selected. The propagate_changes script doesn't need changes since it rsyncs the whole template.

## Changes

* **forever-claude-template** (branching off gabe/skills-migrate):
  - Rename `CLAUDE.md` to `AGENTS.md` as the canonical instructions file. The provisioning setup script copies it to `CLAUDE.md` for claude agents (claude reads CLAUDE.md, not AGENTS.md).
  - Add `agents/hermes/` directory containing: `config.yaml` (model, toolsets, external_skill_dirs overlay), `plugins/` directory with thin Python hook wrappers that call shared scripts, and `setup.sh` provisioning script.
  - Add `agents/claude/setup.sh` provisioning script that copies `AGENTS.md` to `CLAUDE.md` and cleans up hermes-specific files.
  - Move shared hook logic into scripts (e.g. `scripts/agent_setup.sh`, `scripts/guard_commit_rewrite.sh`, `scripts/check_repo_root.sh`) that both agent types' declarations invoke.
  - Update `.mngr/settings.toml`: add `[agent_types.hermes]` and `[agent_types.hermes_main]` (parent_type = "hermes"). Add hermes variants of create templates that use `hermes_main` type and call the hermes setup script via `extra_provision_command`. Update dev template's `extra_provision_command` to also install mngr_hermes plugin.
  - Update `Dockerfile`: install hermes-agent (pip/uv), register mngr_hermes plugin via `mngr plugin add`.
  - Update `.claude/settings.json` SessionStart hooks (or agents/claude/setup.sh) to copy AGENTS.md to CLAUDE.md on session start, so the instructions file stays in sync if updated.
* **apps/minds/** (minds desktop client):
  - Add `AgentType` enum (or similar) to primitives.
  - Update `render_create_form` to include an agent type dropdown.
  - Update `_build_mngr_create_command` to accept agent type and select the appropriate create template (e.g. `--template hermes-docker` instead of `--template docker` when hermes is selected).
  - Update `run_mngr_create` to pass agent type through.
* **No changes to libs/mngr_hermes plugin** -- the existing plugin's HERMES_HOME seeding and env var injection is sufficient. Template provisioning handles all minds-specific hermes configuration.
