# imbue-mngr-skills

Claude Code skills that teach Claude how to use [mngr](https://github.com/imbue-ai/mngr) -- for example, to coordinate with other agents. More skills may be added over time; the current set is listed below.

## Skills

- `/imbue-mngr-skills:message-agent <agent> <what to say>` -- send a message to another mngr agent.
- `/imbue-mngr-skills:wait-for-agent <agent> [follow-up instructions]` -- block until an agent reaches a ready state (WAITING without a permissions reason, DONE, or STOPPED), then carry out the follow-up.
- `/imbue-mngr-skills:find-agent <name or description>` -- resolve an agent name or description to an exact agent name. Used by the other two skills, but invocable on its own.
- `/imbue-mngr-skills:mngr-help` -- when knowing about mngr would help, run `mngr help` right away for context on what mngr does; also points at `mngr ask` (ask mngr questions in plain language).

`message-agent` and `wait-for-agent` use the name you give them when it already looks like an agent name, and fall back to `find-agent` only when it doesn't -- for example when you paste a `mngr/<branch>` name or describe the agent instead of naming it.

## Installation

The easiest way is through mngr:

```bash
mngr extras claude-plugin
```

and choose to install the imbue-mngr-skills plugin.

To install manually with the Claude Code CLI:

```bash
claude plugin marketplace add imbue-ai/mngr
claude plugin install imbue-mngr-skills@imbue-mngr
```
