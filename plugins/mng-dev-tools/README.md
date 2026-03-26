# mng-dev-tools

Development workflow tools for [mng](https://github.com/imbue-ai/mng) users: issue identification, FIXME management, documentation writing, and terminal demos.

## Install

```
claude plugin marketplace add imbue-ai/mng && claude plugin install mng-dev-tools@imbue-mng
```

## Skills

### Issue identification

- **identify-inconsistencies** -- Scan a library for code-level inconsistencies (naming, patterns, structure).
- **identify-doc-code-disagreements** -- Find places where documentation and implementation disagree.
- **identify-style-issues** -- Find divergences from the project's style guide.
- **identify-outdated-docstrings** -- Find docstrings that no longer match what the code does.
- **create-fixmes** -- Create FIXME comments in code from an identified-issues file.
- **create-github-issues-from-file** -- Convert identified issues into GitHub issues.

### FIXME management

- **fix-something** -- Pick a random FIXME from the codebase and fix it. Tracks attempt counts so the same issue isn't retried endlessly.
- **think-of-something-to-fix** -- Guidance for choosing good things to fix when you need ideas.

### Writing

- **writing-docs** -- Guidelines for writing clear, user-facing documentation.
- **writing-specs** -- Guidelines for writing technical specifications and design docs.

### Other

- **wait-for-agent** -- Wait for another mng agent to reach a ready state, then execute follow-up instructions.
- **asciinema-demos** -- Create short terminal demo recordings that visually demonstrate completed work.
