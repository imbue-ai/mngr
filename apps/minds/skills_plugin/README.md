# imbue-studio-skills

Claude Code and Codex skills that teach an agent how to drive [Imbue Studio](https://github.com/imbue-ai/mngr/tree/main/apps/minds): list, create, start, stop, and destroy workspaces, read their backups, and manage sharing, from the user's own machine or from inside a workspace.

## Skills

- `imbue-studio`: how to reach Imbue Studio from wherever the agent is running, how to start the app when it is not running, how to read the live API contract, which permissions a workspace agent needs and how to ask for them, and what an agent must never do with the user's credentials.

Imbue Studio is built on mngr, so install `imbue-mngr-skills` from the same marketplace as well; the studio skill points at `mngr help` and the mngr plugin CLIs rather than repeating them.

## Installation

Through mngr, which offers this plugin alongside the others it knows about:

```bash
mngr extras claude-plugin
```

With the Claude Code CLI directly:

```bash
claude plugin marketplace add imbue-ai/imbue-skills
claude plugin install imbue-studio-skills@imbue-skills
claude plugin install imbue-mngr-skills@imbue-skills
```

With the Codex CLI:

```bash
codex plugin marketplace add imbue-ai/imbue-skills
codex plugin add imbue-studio-skills@imbue-skills
codex plugin add imbue-mngr-skills@imbue-skills
```

## Where this comes from

The source of truth is `apps/minds/skills_plugin/` in [imbue-ai/mngr](https://github.com/imbue-ai/mngr); the published copy in `imbue-ai/imbue-skills` is written only by an automated export from there, so send changes to the mngr repo.
