# imbue-mngr-skills

Claude Code skills that teach Claude how to use [mngr](https://github.com/imbue-ai/mngr) -- for example, to coordinate with other agents. More skills may be added over time; the current set is listed below.

## Skills

- `/imbue-mngr-skills:message-agent <agent> <what to say>` -- send a message to another mngr agent.
- `/imbue-mngr-skills:wait-for-agent <agent> [follow-up instructions]` -- block until an agent reaches a ready state (WAITING without a permissions reason, DONE, or STOPPED), then carry out the follow-up.
- `/imbue-mngr-skills:find-agent <name or description>` -- resolve an agent name or description to an exact agent name. Used by the other two skills, but invocable on its own.
- `/imbue-mngr-skills:mngr-help` -- when you want to run an mngr command but aren't sure which, points you at `mngr help` (browse commands) and `mngr ask` (describe what you want in plain language).

`message-agent` and `wait-for-agent` first try the name you give them verbatim; they only fall back to `find-agent` when that exact name does not match a live agent (for example when you paste a `mngr/<branch>` name or describe the agent instead of naming it).

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
