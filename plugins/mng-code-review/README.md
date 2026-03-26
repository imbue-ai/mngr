# mng-code-review

Automated code review enforcement for [mng](https://github.com/imbue-ai/mng) users.

When installed, a Stop hook can block Claude from finishing until autofix, architecture verification, and conversation review have been run. **The hook is off by default** -- you need to enable it by configuring `stop_hook.enabled_when` in `.reviewer/settings.json` or `.reviewer/settings.local.json`.

## Install

```
claude plugin marketplace add imbue-ai/mng && claude plugin install mng-code-review@imbue-mng
```

## Enabling the stop hook

After installing, enable the stop hook by running:

```
/mng-code-review:reviewer-enable
```

This enables enforcement for all sessions. To only enforce on mng-managed agent sessions:

```
/mng-code-review:reviewer-enable test -n "${MNG_AGENT_STATE_DIR:-}"
```

The argument is a shell expression evaluated before each stop hook invocation. Individual gates can be disabled with `/mng-code-review:reviewer-disable`.

## Skills

- **autofix** -- Iteratively find and fix code issues on a branch. Spawns fresh-context agents for each pass, presents fixes for review, and reverts any you reject.
- **verify-architecture** -- Assess whether the approach on a branch fits existing codebase patterns. Generates independent solution proposals before examining the diff to avoid confirmation bias. Runs once per branch (not per commit), but should be re-run after fundamental architecture changes.
- **verify-conversation** -- Review the conversation transcript for behavioral issues (misleading behavior, disobeyed instructions, feedback worth saving).

## Configuration

- **reviewer-disable** -- Disable all review gates at once.
- **reviewer-autofix-enable / disable** -- Toggle the autofix gate.
- **reviewer-autofix-all-issues / ignore-minor-issues** -- Control issue severity threshold for unattended autofix.
- **reviewer-ci-enable / disable** -- Toggle the CI gate.
- **reviewer-verify-conversation-enable / disable** -- Toggle the conversation review gate.
- **reviewer-verify-architecture-enable / disable** -- Toggle the architecture verification gate.

## How enforcement works

The plugin registers a **Stop** hook that runs every time Claude finishes a response. If `stop_hook.enabled_when` is not configured (or its shell expression exits non-zero), the hook passes through silently. When enabled, if any gate hasn't been satisfied, the hook blocks the session and prompts the agent to run the missing checks.

Gates checked:
- **Autofix** -- per-commit (must re-run after each new commit)
- **Architecture verification** -- per-branch (runs once, persists across commits)
- **Conversation review** -- per-commit
- **CI** -- handled by the full mng stop hook, not this plugin

A safety hatch prevents infinite loops: after 3 consecutive blocks at the same commit, the hook lets the agent through and clears the tracker.

## Agents

- **verify-and-fix** -- Autonomous code verifier and fixer (used by autofix)
- **analyze-architecture** -- Evaluates whether branch changes fit codebase patterns (used by verify-architecture)
- **validate-diff** -- Quick sanity check on a branch's diff (used by autofix and verify-architecture)
- **review-conversation** -- Reviews conversation transcripts for behavioral issues (used by verify-conversation)
