# imbue-mngr-skills

Claude Code skills for coordinating [mngr](https://github.com/imbue-ai/mngr) agents. Once installed, an agent can message a peer, wait for a peer to finish, and resolve a fuzzy agent name or description to an exact agent.

## Skills

- `/imbue-mngr-skills:message-agent <agent> <what to say>` -- send a message to another mngr agent.
- `/imbue-mngr-skills:wait-for-agent <agent> [follow-up instructions]` -- block until an agent reaches a ready state (WAITING without a permissions reason, DONE, or STOPPED), then carry out the follow-up.
- `/imbue-mngr-skills:find-agent <name or description>` -- resolve an agent name or description to an exact agent name. Used by the other two skills, but invocable on its own.

`message-agent` and `wait-for-agent` first try the name you give them verbatim; they only fall back to `find-agent` when that exact name does not match a live agent (for example when you paste a `mngr/<branch>` name or describe the agent instead of naming it).

## Installation

The easiest way is through mngr:

```bash
mngr extras claude-plugin
```

and choose to install the agent-coordination skills.

To install manually with the Claude Code CLI:

```bash
claude plugin marketplace add imbue-ai/mngr
claude plugin install imbue-mngr-skills@imbue-mngr
```
