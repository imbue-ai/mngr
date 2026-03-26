# mng-skills

Code review, architecture verification, conversation analysis, and development workflow tools for [mng](https://github.com/imbue-ai/mng) users.

## Install

```
claude plugin marketplace add imbue-ai/mng && claude plugin install mng-skills@mng-marketplace
```

If you're working in the mng repo itself, the plugin is offered automatically when you trust the project folder.

## What's included

### Code review

- **autofix** -- Iteratively find and fix code issues on a branch. Spawns fresh-context agents for each pass, presents fixes for review, and reverts any you reject.
- **verify-architecture** -- Assess whether the approach on a branch fits existing codebase patterns. Generates independent solution proposals before examining the diff to avoid confirmation bias.
- **verify-conversation** -- Review the conversation transcript for behavioral issues (misleading behavior, disobeyed instructions, feedback worth saving).
- **reviewer-autofix-enable / disable** -- Toggle the autofix gate.
- **reviewer-autofix-all-issues / ignore-minor-issues** -- Control issue severity threshold for unattended autofix.
- **reviewer-ci-enable / disable** -- Toggle the CI gate.
- **reviewer-verify-conversation-enable / disable** -- Toggle the conversation review gate.

### Development workflow

- **fix-something** -- Pick a random FIXME from the codebase and fix it. Tracks attempt counts so the same issue isn't retried endlessly.
- **wait-for-agent** -- Wait for another mng agent to reach a ready state, then execute follow-up instructions.
- **identify-inconsistencies** -- Scan a library for code-level inconsistencies (naming, patterns, structure).
- **identify-doc-code-disagreements** -- Find places where documentation and implementation disagree.
- **identify-style-issues** -- Find divergences from the project's style guide.
- **identify-outdated-docstrings** -- Find docstrings that no longer match what the code does.
- **create-fixmes** -- Create FIXME comments in code from an identified-issues file.
- **create-github-issues-from-file** -- Convert identified issues into GitHub issues.

### Writing

- **writing-docs** -- Guidelines for writing clear, user-facing documentation.
- **writing-specs** -- Guidelines for writing technical specifications and design docs.

### Other

- **think-of-something-to-fix** -- Guidance for choosing good things to fix when you need ideas.
- **asciinema-demos** -- Create short terminal demo recordings that visually demonstrate completed work.

## Hooks

The plugin registers a **Stop** hook that blocks sessions from finishing until autofix and conversation review have been run (unless disabled via the reviewer configuration skills above). Configuration is stored in `.reviewer/settings.json` with local overrides in `.reviewer/settings.local.json`.

## Agents

The plugin provides 4 agents used by the skills above:

- **verify-and-fix** -- Autonomous code verifier and fixer (used by autofix)
- **analyze-architecture** -- Evaluates whether branch changes fit codebase patterns (used by verify-architecture)
- **validate-diff** -- Quick sanity check on a branch's diff (used by autofix and verify-architecture)
- **review-conversation** -- Reviews conversation transcripts for behavioral issues (used by verify-conversation)
