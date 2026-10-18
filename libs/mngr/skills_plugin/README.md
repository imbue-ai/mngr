# imbue-mngr-skills

Claude Code and Codex skills that teach an agent how to use [mngr](https://github.com/imbue-ai/mngr), for example to coordinate with other agents.

## Skills

- `/imbue-mngr-skills:message-agent <agent> <what to say>`: send a message to another mngr agent.
- `/imbue-mngr-skills:wait-for-agent <agent> [follow-up instructions]`: block until an agent reaches a ready state (WAITING without a permissions reason, DONE, or STOPPED), then carry out the follow-up.
- `/imbue-mngr-skills:find-agent <name or description>`: resolve an agent name or description to an exact agent name. Used by the other two skills, but invocable on its own.
- `/imbue-mngr-skills:mngr-help`: when knowing about mngr would help, run `mngr help` right away for context on what mngr does; also points at `mngr ask` (ask mngr questions in plain language).

`message-agent` and `wait-for-agent` use the name you give them when it already looks like an agent name, and fall back to `find-agent` only when it doesn't, for example when you paste a `mngr/<branch>` name or describe the agent instead of naming it.

## Installation

Through mngr, which offers this plugin alongside the others it knows about:

```bash
mngr extras claude-plugin
```

With the Claude Code CLI directly:

```bash
claude plugin marketplace add imbue-ai/imbue-skills
claude plugin install imbue-mngr-skills@imbue-skills
```

With the Codex CLI:

```bash
codex plugin marketplace add imbue-ai/imbue-skills
codex plugin add imbue-mngr-skills@imbue-skills
```

## Where this comes from

The source of truth is `libs/mngr/skills_plugin/` in [imbue-ai/mngr](https://github.com/imbue-ai/mngr); the published copy in `imbue-ai/imbue-skills` is written only by an automated export from there, so send changes to the mngr repo.
