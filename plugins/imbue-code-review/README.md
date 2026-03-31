# imbue-code-review

Automated code review enforcement for Claude Code. When enabled, a Stop hook blocks Claude from finishing until autofix, architecture verification, and conversation review have been run.

**The hook is off by default.** Enable it after installing.

## Install

```
claude plugin marketplace add imbue-ai/mngr && claude plugin install imbue-code-review@imbue-mngr
```

## Enabling the stop hook

After installing, enable enforcement:

```
/imbue-code-review:reviewer-enable
```

The argument is an optional shell expression controlling when enforcement fires. For example, to only enforce when a specific env var is set:

```
/imbue-code-review:reviewer-enable test -n "${MY_AGENT_ENV_VAR:-}"
```

Individual gates can be disabled with `/imbue-code-review:reviewer-disable`.

## Skills

- **autofix** -- Iteratively find and fix code issues on a branch. Spawns fresh-context agents for each pass, presents fixes for review, and reverts any you reject.
- **verify-architecture** -- Assess whether the approach on a branch fits existing codebase patterns. Generates independent solution proposals before examining the diff to avoid confirmation bias. Runs once per branch (not per commit), but should be re-run after fundamental architecture changes.
- **verify-conversation** -- Review the conversation transcript for behavioral issues (misleading behavior, disobeyed instructions, feedback worth saving).

## Configuration

- **reviewer-enable** -- Enable the stop hook. Optionally takes a shell expression for when to enforce.
- **reviewer-disable** -- Disable all review gates at once.
- **reviewer-init-categories** -- Copy the default issue categories to `.reviewer/` for customization.
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

A safety hatch prevents infinite loops: after 3 consecutive blocks at the same commit, the hook lets the agent through and clears the tracker.

## Issue categories

The plugin ships default issue categories. To customize them for your project, run `/imbue-code-review:reviewer-init-categories` to copy the defaults to `.reviewer/code-issue-categories.md` and `.reviewer/conversation-issue-categories.md`, then edit directly. The skills check `.reviewer/` first, falling back to plugin defaults.

## Agents

- **verify-and-fix** -- Autonomous code verifier and fixer (used by autofix)
- **analyze-architecture** -- Evaluates whether branch changes fit codebase patterns (used by verify-architecture)
- **validate-diff** -- Quick sanity check on a branch's diff (used by autofix and verify-architecture)
- **review-conversation** -- Reviews conversation transcripts for behavioral issues (used by verify-conversation)
