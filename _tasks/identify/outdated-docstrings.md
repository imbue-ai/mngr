# Outdated docstrings in mngr (identified on 2026-05-10)

Run as part of tkt-run-identify-outdated-docstrin-fv1z. Scope: `libs/mngr/imbue/mngr/` core (primitives.py, errors.py, api/, interfaces/) and a sampling of `libs/mngr_claude/` (headless_claude_agent.py, plugin headers). This is a sampling pass, not exhaustive; subsequent runs (n=2, ...) can broaden to remaining api/ files, providers/, agents/, hosts/host.py interior, other plugins.

Methodology: Read source, compared each docstring claim against the function body and its callers. Flagged only triple-quoted docstrings where the claim is contradicted by current behavior or is plainly under-specified relative to the implementation. Did not flag missing docstrings, style nits, or inline parameter comments (those belong to other identify-* runs).

`non_issues.md` consulted; none of the findings below fall under the listed exemptions.

## 1. imbue.mngr.interfaces.host.OnlineHostInterface.record_activity

### Current:

```
Record activity of the given type; only BOOT and CREATE are valid here.
```

### Problem(s):

The implementation in `imbue.mngr.hosts.host.Host.record_activity` (libs/mngr/imbue/mngr/hosts/host.py:498-511) accepts **only** `ActivitySource.BOOT` and raises `InvalidActivityTypeError` for everything else, including CREATE. Its own docstring says "Only BOOT is valid for host-level activity." So the interface docstring lists a valid value (CREATE) that is actually rejected at runtime. Callers reading the interface will write code that throws.

### Recommendation:

```
Record activity of the given type. Only BOOT is valid for host-level
activity; other ActivitySource values raise InvalidActivityTypeError.
Per-agent activity (USER, AGENT, etc.) is recorded via
AgentInterface.record_activity, not here.
```

### Decision:

Accept


## 2. imbue.mngr.errors.AgentStartError

### Current:

```
Failed to start an agent's tmux session.
```

### Problem(s):

`AgentStartError` is raised in non-tmux contexts as well. `libs/mngr/imbue/mngr/hosts/host.py:710` and `:715` raise it when the agent's work directory does not exist (purely a filesystem precondition; tmux is never involved). The docstring narrows the error to one specific failure mode and will mislead anyone trying to handle/test the broader cases.

### Recommendation:

```
Failed to start an agent.

Raised for any precondition that prevents the agent from launching --
e.g. a missing tmux session, a missing work directory, or a process
failure during start. Consult the exception's reason field for the
specific cause.
```

### Decision:

Accept


## 3. imbue.mngr.interfaces.agent.AgentInterface.send_message

### Current:

```
Send a message to the running agent via its stdin.
```

### Problem(s):

The phrase "via its stdin" is wrong for every concrete implementation in the tree:

- Interactive agents (the only ones that *can* accept messages live) implement `send_message` in `imbue.mngr.agents.base_agent.BaseAgent.send_message` (libs/mngr/imbue/mngr/agents/base_agent.py:359-379), which sends keystrokes to the tmux pane via `tmux send-keys` (with paste-detection synchronization for agents like Claude). This is the agent's PTY, not "stdin" in any pipe-oriented sense.
- Headless / streaming-headless agents do not accept live messages at all -- they either stage the message to a file before start (`stage_initial_message`) or raise. The comment block in `create.py` (libs/mngr/imbue/mngr/api/create.py:226-232) explicitly says "Headless agents cannot receive messages that way: they communicate via stdout/stdin pipes, so wait_for_ready_signal / send_message both raise."

So the only agents for which `send_message` actually does something do not use stdin, and the agents that conceivably use stdin pipes raise rather than implement the call.

### Recommendation:

```
Send a message to the running agent.

For interactive agents this is delivered to the agent's tmux pane
(typically via `tmux send-keys`, optionally with paste-detection
synchronization). Headless agents do not support live messages and
raise; their initial prompt is staged on disk before start instead
(see StreamingHeadlessAgentMixin.stage_initial_message).
```

### Decision:

Accept


## 4. imbue.mngr.api.exec.exec_command_on_outer_hosts

### Current:

```
... The default cwd is the SSH user's home directory on the outer host.
```
(final line of the docstring at libs/mngr/imbue/mngr/api/exec.py:336)

### Problem(s):

The function does not in fact pin the default cwd to the SSH user's home directory. The code passes `None` to `outer.execute_stateful_command` when `cwd` is unset, with an inline comment that says "None means the connector's default" (libs/mngr/imbue/mngr/api/exec.py:388-389). What the connector chooses is implementation-defined per outer host (e.g. SSH, local, container). For an SSH connector the default tends to be the SSH user's home, but the docstring promises a universal behavior the function does not enforce.

### Recommendation:

```
... When cwd is not provided, the outer host's connector chooses its
default working directory (for SSH-backed outers this is typically the
SSH user's home directory, but other connector types may differ).
```

### Decision:

Accept


## 5. (Borderline -- flagged for triage, not a strict docstring violation)

`imbue.mngr.api.list.list_agents` (libs/mngr/imbue/mngr/api/list.py:134) carries an inline parameter comment `# If specified, only list agents from these providers (NOT IMPLEMENTED YET)`. The feature *is* implemented: `provider_names` propagates through both batch and streaming paths into `list_provider_names_to_load`, which filters providers by name (libs/mngr/imbue/mngr/api/providers.py:110-160).

This is not a triple-quoted docstring, so it is outside the strict scope of "outdated docstrings"; it is included here as a related signal worth picking up in the doc-code-disagreements run (tkt-run-identify-doc-code-disagree-9xmt). If the interpretation is widened to include parameter-doc comments, this becomes a strong finding -- callers reading the signature will believe the filter is a no-op.

### Decision:

Defer to tkt-run-identify-doc-code-disagree-9xmt
