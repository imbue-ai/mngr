A create template may now set a field on the agent type the create resolves to, not just a
`mngr create` option. This lets a template describe an agent's *role* without naming the
harness that will run it: a role writes `output_style = "..."` or
`append_system_prompt__extend = [...]` once and it lands on whichever type the template stack
selected. Previously the only way to express either was `agent_args`, which is raw argv and
therefore harness-specific, so a role that needed one had to be duplicated per harness.

An output style is a markdown file whose frontmatter sets `name:`; `output_style` takes that
display name, not a filename. The name is resolved and validated during provisioning, and an
unknown name fails the create with the available names listed, rather than silently launching
an agent with no style applied.

Routing happens after every template applies, because a harness template is what sets the
type. Keys compile into settings entries and reuse the existing template-contributed-settings
fold, so the operator suffixes behave exactly as they do everywhere else: a bare key assigns
(the last role in the stack wins) and `__extend` accumulates. Keys are combined per field
before being emitted, because each settings entry is applied against the base config
independently -- two `__extend` entries for one field would each extend the empty base and the
later would simply win, dropping the earlier role's contribution.

A key that is neither an option nor a field on that type now RAISES, naming the template, the
type, and which types do support it. Previously it was silently dropped, so a typo -- or a role
stacked onto a harness that could not honour it -- produced an agent that quietly ignored part
of its configuration. The two role fields live on the harness config subclasses rather than on
the base `AgentTypeConfig`, so a harness with no support for them has no field to route to and
the create fails naming the template instead of launching a misconfigured agent.

Stacking create templates that declare conflicting base `type`s is now rejected up front,
rather than silently letting the last template win. A create resolves to exactly one base
type, so a stack like `-t worker -t codex` (a claude role plus a codex one) is an ambiguous,
always-wrong request and now fails naming the conflicting templates. Aliases are normalised
first (so two names for the same base do not conflict), templates that leave `type` unset
never participate, and an explicit `--type` on the command line still overrides every
template's type (it is authoritative), so only the templates-decide-the-type case is guarded.

The in-process message API gains `send_key_chord_to_agents`, the keystroke sibling of
`send_message_to_agents`: it presses a single tmux key token (e.g. `M-q`) into a resolved set of
agents' panes with the same host-grouped concurrent fan-out and the same per-agent `message.lock`
serialization as a text send. The lock is what keeps a chord from landing between a concurrent
message's paste and its Enter. Keystroke-driven agents (`SendKeysAgent` / the interactive TUI
agents) implement it via the new `SupportsKeyChordMixin.press_key_chord`; agents whose input is an
API rather than a tmux pane (pi, opencode) are refused per-agent with a clear error. The Minds
workspace UI uses this to deliver claude's native queue-flush chord.

`mngr create` now waits for a newly-created agent's readiness signal even when there is no
initial message to deliver. Previously the ready-signal wait only ran on the with-a-message
path; a no-message create (how the Minds chat UI creates every chat) returned the instant the
process was spawned. For a slow-booting harness that meant the agent surfaced as live before it
could accept input -- most visibly pi, which writes its session and model-state files several
seconds after launch, so a fresh pi chat showed a wrong status and an empty model bar and
dropped an early message into a race. The wait uses the same per-harness `wait_for_ready_signal`
the message path already uses (claude/codex poll for the composer prompt in the pane; pi waits
for its session-started sentinel file). Its base implementation only runs the start action, so a
plain `command` agent that does not override readiness detection still starts with no added wait.
