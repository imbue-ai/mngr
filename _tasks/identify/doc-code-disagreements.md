# Doc and code disagreements in the mngr library (identified on 2026-05-10)

Run as part of tkt-run-identify-doc-code-disagree-9xmt. Scope: `libs/mngr/imbue/mngr/` and `libs/mngr/docs/` (README, conventions.md, concepts/*.md). Did not exhaustively cover docs/commands/, docs/core_plugins/, plugin READMEs, or `future_specs/` (the SKILL explicitly says not to chase old spec drift).

Methodology: Read user-facing docs and matched each definite claim against the current code. Flagged disagreements where the docs describe behavior that is contradicted (not merely under-specified) by the implementation. Skipped `[future]`-tagged statements and `non_issues.md` exemptions.

`non_issues.md` consulted; none of the findings below fall under the listed exemptions.

## 1. "mngr message just writes to stdin" -- the actual mechanism is tmux send-keys

Description: `docs/concepts/agents.md:53` (Capabilities section) says:

> Agents can be sent messages via `mngr message` (for example, to provide user input or commands). **This applies to all unix process (since we're just writing to stdin).**

The implementation does not write to stdin. For interactive agents, `BaseAgent.send_message` in `libs/mngr/imbue/mngr/agents/base_agent.py:359-379` delivers messages by injecting keystrokes into the agent's tmux pane via `tmux send-keys` -- with paste-detection synchronization for agents like Claude that echo input. For headless agents, messages are not delivered live at all: `stage_initial_message` writes the prompt to a file (`.mngr-prompt` in the agent state dir) which the agent's launch command reads at startup, and `send_message` raises if called (see comment in `libs/mngr/imbue/mngr/api/create.py:226-232`).

This is a load-bearing disagreement. The doc's "we're just writing to stdin" implies any unix process that reads stdin can be messaged, which is not how mngr's transport works -- the agent must be running in a tmux pane mngr controls, and live messaging is unsupported for headless agents entirely.

Recommendation:

> Agents can be sent messages via `mngr message` (for example, to provide user input or commands). For interactive agents the message is delivered into the agent's tmux pane (using `tmux send-keys`, with paste-detection synchronization for agents like Claude Code that echo input). Headless agents do not support live messages; their initial prompt is staged on disk and read at startup. Either way, the agent process itself does not need to do anything special to receive messages -- it just needs to be running in the tmux session mngr started for it.

Decision: Accept


## 2. agents.md lists 5 lifecycle states; the enum has 6

Description: `docs/concepts/agents.md:73-79` ("Lifecycle" section) enumerates the agent lifecycle states as: **stopped, running, waiting, replaced, done** (5 states).

The enum in `libs/mngr/imbue/mngr/primitives.py:229-239` (`AgentLifecycleState`) defines 6: **STOPPED, RUNNING, WAITING, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE, DONE**. The extra `RUNNING_UNKNOWN_AGENT_TYPE` is a real state surfaced to users (it appears in `mngr list` when an agent is running but its type is not in the local config), and it has a documenting code comment explaining why.

Recommendation: Add a bullet for `running_unknown_agent_type` explaining the "running but type not in local config" case. The comment in the enum is a good seed: "the agent is running but our configuration doesn't have an entry for that agent type (e.g., if it was launched remotely or by someone else)".

Decision: Accept


## 3. hosts.md lifecycle table misses the `UNAUTHENTICATED` state

Description: `docs/concepts/hosts.md:73-83` lists 9 host lifecycle states (building, starting, running, stopping, paused, stopped, crashed, failed, destroyed) and includes a state diagram.

The enum in `libs/mngr/imbue/mngr/primitives.py:214-226` (`HostState`) defines 10 states: the 9 above plus `UNAUTHENTICATED`. This state is reachable in practice -- for example, the providers code uses it for hosts whose credentials cannot be loaded -- and surfaces in `mngr list` output via the same HOST STATE column the docs are explaining.

Recommendation: Add an `UNAUTHENTICATED` row to the lifecycle table describing when it occurs (the provider has the host on record but cannot authenticate to it). Update the rough state diagram if it's intended to be exhaustive.

Decision: Accept


## 4. idle_detection.md mode table misses the `CUSTOM` mode

Description: `docs/concepts/idle_detection.md:23-34` describes idle modes via a feature-matrix table covering: io, user, agent, ssh, create, boot, start, run, disabled (9 modes).

The enum in `libs/mngr/imbue/mngr/primitives.py:79-91` (`IdleMode`) defines 10 modes; the table is missing `CUSTOM`. The fact that `custom` is named at all in the enum strongly suggests it is a public-facing value users can pass.

Recommendation: Either add a `custom` row to the table (explaining what activity it counts and how callers configure the underlying `activity_sources` set), or, if `custom` is intended to be opaque (set indirectly when the user passes individual `--activity` flags rather than as a named mode), mention it in a short paragraph below the table.

Decision: Accept


## 5. conventions.md says names cannot contain underscores -- they can

Description: `docs/conventions.md:13` says:

> Names are human-readable strings that can contain letters, numbers, and hyphens (no underscores, spaces, etc because they are used for DNS)

But `SafeName` in `libs/mngr/imbue/mngr/primitives.py:268-286` enforces the regex `^[a-zA-Z0-9][a-zA-Z0-9_-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$`, which explicitly **allows underscores in the middle**. Empirically verified: `SafeName("foo_bar")` and `SafeName("foo_bar_baz")` both succeed; only `foo bar` (with a space) is rejected. The `SafeName.__doc__` and the `InvalidName` error message both correctly say "dashes and underscores allowed in the middle."

The doc's parenthetical ("because they are used for DNS") suggests an old DNS-only constraint that no longer applies. Names are used for tmux session names (which tolerate underscores) and filesystem paths (which tolerate underscores).

Recommendation: Update the line to:

> Names are human-readable strings that can contain letters, numbers, dashes, and underscores (dashes/underscores must be in the middle, not at the start or end). Other punctuation and whitespace are not allowed.

If a DNS-safe sub-namespace is still required somewhere (e.g. for hostnames published via a public DNS-style scheme), call out that specific case rather than implying it applies to all names.

Decision: Accept


## 6. api/list.py parameter comment claims `provider_names` is "NOT IMPLEMENTED YET"

Description: `libs/mngr/imbue/mngr/api/list.py:134` has a parameter-doc inline comment immediately above the parameter declaration:

```python
# If specified, only list agents from these providers (NOT IMPLEMENTED YET)
provider_names: tuple[str, ...] | None = None,
```

The feature is in fact implemented. `provider_names` is threaded through both `_list_agents_batch` and `_list_agents_streaming` (both call `list_provider_names_to_load(mngr_ctx, provider_names)`), and `list_provider_names_to_load` in `libs/mngr/imbue/mngr/api/providers.py:110-160` honors it via `provider_filter` (the early `if provider_filter is not None and str(name) not in provider_filter: continue` branch). The same parameter is also used by `_maybe_write_full_discovery_snapshot` to decide whether a listing is "full" and worth snapshotting.

This is a parameter-doc comment rather than a triple-quoted docstring, so it was deferred here from tkt-run-identify-outdated-docstrin-fv1z (outdated-docstrings run). It is a real and load-bearing doc-vs-code disagreement: callers reading the source will believe the filter is a no-op and write wrappers around it.

Recommendation: Remove "(NOT IMPLEMENTED YET)" from the comment. Keep the rest of the description -- it accurately describes the filter.

Decision: Accept


## 7. (Borderline) hosts.md outer-host table vs. code coverage of providers

Description: `docs/concepts/hosts.md:48-60` provides a per-provider table for outer-host accessibility. Spot-checked against `imbue/mngr/providers/registry.py` and the providers under `libs/mngr_*/` -- the named providers (`local`, `ssh`, `docker`, `mngr_modal`, `mngr_vps_docker`, `mngr_vultr`, `mngr_imbue_cloud`) all exist. However, the repo also contains `mngr_lima` (libs/mngr_lima/) which is missing from the table. Not necessarily a disagreement (the table can be intentionally non-exhaustive), but worth confirming whether `mngr_lima` should be listed.

Recommendation: Either add a row for `mngr_lima` (likely "the local machine", since lima provisions local VMs) or add a note above the table that it lists only the most common providers and that plugins may add their own.

Decision: Defer (low-impact; verify outer-host behavior in mngr_lima before adding a row)
