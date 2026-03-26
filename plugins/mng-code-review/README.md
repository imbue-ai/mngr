# mng-code-review

Automated code review enforcement for [mng](https://github.com/imbue-ai/mng) users.

**This plugin enforces code quality by default.** When installed, a Stop hook blocks Claude from finishing until autofix, architecture verification, and conversation review have been run. Enforcement is on by default but can be disabled with `/mng-code-review:reviewer-disable`, or individual gates can be toggled with the configuration skills below.

## Install

```
claude plugin marketplace add imbue-ai/mng && claude plugin install mng-code-review@imbue-mng
```

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

The plugin registers a **Stop** hook that runs every time Claude finishes a response. If any enabled gate hasn't been satisfied, the hook exits non-zero, which prevents Claude from stopping and prompts it to run the missing checks.

Gates checked:
- **Autofix** -- per-commit (must re-run after each new commit)
- **Architecture verification** -- per-branch (runs once, persists across commits)
- **Conversation review** -- per-commit
- **CI** -- handled by the full mng stop hook, not this plugin

Configuration is stored in `.reviewer/settings.json` with local overrides in `.reviewer/settings.local.json`. Use the reviewer-* skills to toggle gates without editing JSON directly.

## Agents

- **verify-and-fix** -- Autonomous code verifier and fixer (used by autofix)
- **analyze-architecture** -- Evaluates whether branch changes fit codebase patterns (used by verify-architecture)
- **validate-diff** -- Quick sanity check on a branch's diff (used by autofix and verify-architecture)
- **review-conversation** -- Reviews conversation transcripts for behavioral issues (used by verify-conversation)
