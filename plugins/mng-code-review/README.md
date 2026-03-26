# mng-code-review

Automated code review enforcement for [mng](https://github.com/imbue-ai/mng) users.

**This plugin enforces code quality by default.** When installed, a Stop hook blocks Claude from finishing until autofix and conversation review have been run. Enforcement is on by default but individual gates can be disabled per-session with the configuration skills below.

## Install

```
claude plugin marketplace add imbue-ai/mng && claude plugin install mng-code-review@mng-marketplace
```

## Skills

- **autofix** -- Iteratively find and fix code issues on a branch. Spawns fresh-context agents for each pass, presents fixes for review, and reverts any you reject.
- **verify-architecture** -- Assess whether the approach on a branch fits existing codebase patterns. Generates independent solution proposals before examining the diff to avoid confirmation bias.
- **verify-conversation** -- Review the conversation transcript for behavioral issues (misleading behavior, disobeyed instructions, feedback worth saving).
- **reviewer-autofix-enable / disable** -- Toggle the autofix gate.
- **reviewer-autofix-all-issues / ignore-minor-issues** -- Control issue severity threshold for unattended autofix.
- **reviewer-ci-enable / disable** -- Toggle the CI gate.
- **reviewer-verify-conversation-enable / disable** -- Toggle the conversation review gate.

## How enforcement works

The plugin registers a **Stop** hook that runs every time Claude finishes a response. If autofix or conversation review hasn't been completed, the hook exits non-zero, which prevents Claude from stopping and prompts it to run the missing checks.

Configuration is stored in `.reviewer/settings.json` with local overrides in `.reviewer/settings.local.json`. Use the reviewer-* skills to toggle gates without editing JSON directly.

## Agents

- **verify-and-fix** -- Autonomous code verifier and fixer (used by autofix)
- **analyze-architecture** -- Evaluates whether branch changes fit codebase patterns (used by verify-architecture)
- **validate-diff** -- Quick sanity check on a branch's diff (used by autofix and verify-architecture)
- **review-conversation** -- Reviews conversation transcripts for behavioral issues (used by verify-conversation)
