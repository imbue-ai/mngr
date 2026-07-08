# Agent capability mixins

A design for making each agent-type plugin's *capabilities* a fact the code carries,
rather than a table maintained by hand. Today the [parity spec](spec.md) tracks which
backend implements what in a markdown matrix; that matrix drifts (e.g. it lists session
preservation as claude-only, but it is now in all five agents). This replaces the
hand-maintained matrix with one **derived** from the code, so adding a capability to an
agent updates the matrix automatically, and a test fails if the doc and code disagree.

## What is and isn't a "capability"

Most parity dimensions are **universal**: every real agent must do them, and only the
*mechanism* differs (launch isolation, idle gating, readiness, input delivery, auth, config
isolation, resume, "mngr owns all start dialogs"). A marker that every agent carries
distinguishes nothing, and the interesting content -- *how* each does it -- is not capturable
by a present/absent flag. These stay as prose in the parity spec.

A **capability** here is narrower: a discrete unit of functionality that an agent could have
or lack, where membership is a structural fact about the agent's code. A capability is worth
tracking even when *all five* agents currently have it (e.g. raw transcript, common
transcript, session preservation are each tracked independently): an all-`Y` row still
documents the baseline a new port must hit and distinguishes the next port that lacks it.
Related capabilities may be lumped into one row where finer detail adds nothing, but the
named ones above stay independent. Folding in the refinements from design review:

- Idle gating and subagent-aware idle gating are **one** universal requirement (report
  RUNNING/WAITING correctly, including under subagents), not a capability.
- Trust handling, onboarding NUX, and the codex update-prompt suppression are all one
  universal requirement: **mngr owns all start dialogs.** The agent shows no native blocking
  dialog on start, so `mngr create some-agent --message '...'` always delivers the message.
  mngr suppresses each native dialog and owns the decision; only the *resolution* differs by
  mode (interactive -> pull it into an mngr-managed prompt; unattended -> pick a default). This
  is required in *both* modes, so it is universal, not part of any capability.
- "Extra agent subtypes" is a registry fact (how many types a plugin registers), not a
  property of any agent class.

The matrix has three cell states: `Y` (present), `-` (applicable but absent), and `n/a`
(the capability does not apply to that *kind* of agent). `n/a` is **derived from the code**,
never hand-declared: each capability carries a `scope`, each agent's kind is read from marker
mixins, and a cell is `n/a` exactly when the scope excludes the kind -- so `n/a` keeps the same
"zero hand-maintained data" property as `Y`/`-` (see [Capability scope](#capability-scope-the-na-state)).

This reverses the original binary design, and the reversal is deliberate. Once the matrix grew
to cover non-standard kinds -- the headless variants and the bare `command` / `headless_command`
runners -- some capabilities became genuinely inapplicable to a *whole kind*: non-interactive
`headless_output` on an interactive agent, live-session `session_resume` on a headless agent, or
a CLI install / version / usage concern on a bare shell command. Marking those `n/a` is more
honest than `-` (which would imply "could have it, just doesn't"), and because the kind is itself
a code-detectable fact, it stays fully derived.

`n/a` is for *kind*-level inapplicability only. An *instance*-level gap is still handled
honestly without it: pi has no tool-approval gate, but rather than marking permissions `n/a`,
pi *implements* Unattended operation degenerately (auto-allow is always on; explicitly setting
it off is a hard error, since pi cannot honor a gate) and *implements* the `waiting_reason`
field with a single-value enum (a real extension point for if pi ever gains an approval gate).
What pi genuinely lacks -- a per-resource allow/deny/ask policy -- is an honest `-`.

### The capabilities

The live, per-agent matrix and the one-line description of each capability are the **generated**
doc `libs/mngr/docs/concepts/agent_capabilities.md` (regenerate with
`just regenerate-agent-capabilities-doc`, i.e. `scripts/make_agent_capabilities_doc.py`); that
doc is the source of truth. This section keeps only the design reasoning behind a few of the
rows that is not obvious from the matrix itself.

The crucial split: **Unattended operation** is "can complete a whole run with no human." Its
*start* dialogs are already handled by the universal mngr-owned-dialogs requirement (which
picks defaults in unattended mode), so the one interactive point left to this capability is
the **in-run tool-approval prompt** -- auto-allowing it is what makes remote / scheduled /
headless agents work at all, the load-bearing capability. All five have it (pi degenerately).
A **per-resource permission policy** (allow/deny/ask per tool) is a refinement on top, present
only for antigravity, opencode, and codex -- claude exposes only blanket auto-allow hooks (no
per-tool config), and pi has no approval gate. So *two* agents (claude and pi) have unattended
operation without a policy, which is exactly why these must be *separately* detectable and
cannot share one mixin (an agent inheriting a combined mixin would falsely claim the policy).
Two small mixins is the price of honest per-aspect detection.

Where each agent's policy lives (verified against the code): antigravity
a `permissions` block in `settings_overrides`; opencode a `permission` block via
`config_overrides`; codex `sandbox_mode` / `approval_policy` / `config_overrides`. claude
routes permissions through blanket `"*"` hooks with no structured per-tool surface, so it is an
honest `-`.

## Membership has two honest shapes

A class mixin is the right tool only for capabilities that live on the **agent class**. The
last three rows live on a **plugin module** as pluggy hookimpls; forcing them into a class
mixin would be decorative, since the behavior isn't on the class. But their membership is
already programmatic: *does a plugin register that hookimpl?* So the design models membership
two ways under one roof:

- **class-level**: `issubclass(agent_class, CapabilityMixin)`.
- **module-level**: a plugin implements hookimpl *X* (deploy: `get_files_for_deploy`;
  `waiting_reason`: `agent_field_generators`).

Usage tracking is a module-level capability with one extra twist: it lives in a **separate
sibling plugin** (`mngr_<harness>_usage`, distinct from the harness's own plugin) that
registers `on_after_provisioning` + `aggregate_usage_source` for the harness's source name.
So its detector asks "does any plugin claim this agent's usage source?" rather than inspecting
the agent's own plugin -- still the module-level shape, just keyed by source name across all
registered plugins. antigravity is the lone `-` (deferred).

All shapes are detected by a single capability registry, so the matrix has one generator
regardless of which a capability uses.

## Capability scope (the `n/a` state)

Detection answers *does this agent have the capability?* Scope answers a prior question:
*does the capability even apply to this kind of agent?* They are orthogonal -- a capability
can be out of scope (`n/a`) regardless of whether the class happens to inherit its mixin.

Each capability declares a `scope`, derived from code-detectable agent-kind traits (never
hand-maintained per agent):

| Scope | Applies to | Example capabilities |
|---|---|---|
| `ALL` | every agent | `unattended_operation`, `deploy_contributions`, `live_output` |
| `CLI_BACKED_ONLY` | agents that wrap a specific CLI | transcripts, `auto_install`, `permission_policy`, `version_management`, `usage_tracking` |
| `INTERACTIVE_ONLY` | CLI-backed and not headless | `waiting_reason_field`, `session_resume` |
| `HEADLESS_ONLY` | headless agents | `headless_output` |

The kind traits come from two positive marker mixins: `HeadlessAgentMixin` (headless) and
`CliBackedAgentMixin` (wraps a specific external coding-model CLI, vs. a bare `command` /
`headless_command` runner). `CLI_BACKED_ONLY` is derived positively (`is_cli_backed`), so a bare
command runner is simply *the agent without that marker* -- it needs no command-specific class of
its own. `headless_output` is scoped `HEADLESS_ONLY` because exposing `output()` non-interactively
is meaningless for an interactive agent.

`unattended_operation` stays `ALL`-scope but reads two ways: an interactive coding agent earns it
by auto-allowing in-run tool prompts (`HasUnattendedModeMixin` declared on the agent), while
headless and bare-command agents have it *by construction* -- they have no prompt to gate on, so
they declare the mixin trivially (`BaseHeadlessAgent`, and `CommandAgent` directly). A future
coding agent that did not auto-allow would correctly show `-`.

A cell renders `n/a` when the capability is out of scope for the agent's kind. One subtlety:
a class mixin can be *inherited* by a kind that the scope excludes -- e.g. `headless_claude`
inherits `HasSessionAdoptionMixin` from `ClaudeAgent` but is headless, so the interactive-only
`session_resume` is `n/a` for it -- so an out-of-scope class-mixin hit renders `n/a` rather than
erroring. The deliberately-registered kinds (a field generator keyed by agent type, a usage
source, a deploy hookimpl) cannot be inherited by accident, so an out-of-scope hit *there* means
the scope is wrong and rendering raises -- a drift guard.

## The capability registry

One module (`scripts/make_agent_capabilities_doc.py`) declares the
capabilities and how to detect each. It is dev-only tooling (it generates the matrix
doc and drift-guards it via `--check`), so it lives in `scripts/` rather than the shipped
`mngr` wheel; the capability *mixins* it references stay in `imbue.mngr.interfaces.agent`,
since agent classes inherit them at runtime. Sketch:

```python
class AgentCapability(FrozenModel):
    key: str                       # matrix row name, stable
    description: str               # one line: what it does, and whether a
                                   # new port normally wants it
    detection_kind: CapabilityDetectionKind   # CLASS_MIXIN | FIELD_GENERATOR |
                                              # PLUGIN_HOOKIMPL | USAGE_SOURCE
    scope: CapabilityScope = CapabilityScope.ALL   # which agent kinds it applies to
    mixin: type | None = None      # for CLASS_MIXIN detection
    hook_name: str | None = None   # for PLUGIN_HOOKIMPL detection

# class-level capability detected by a shared marker that two contract mixins inherit
LIVE_OUTPUT = AgentCapability(
    key="live_output",
    description="Live in-progress view of the agent's output before a turn completes. "
    "Lowest-priority; only needed if a consuming UI wants live streaming.",
    detection_kind=CapabilityDetectionKind.CLASS_MIXIN,
    mixin=SupportsLiveOutputMixin,
)

# module-level capability: detected by hookimpl presence
DEPLOY_CONTRIBUTIONS = AgentCapability(
    key="deploy_contributions",
    description="Bakes config/cred files + env vars into a `mngr schedule` "
    "image. Only needed if the agent runs under `mngr schedule`.",
    detection_kind=CapabilityDetectionKind.PLUGIN_HOOKIMPL,
    hook_name="get_files_for_deploy",
)
```

Detection is a `CapabilityDetectionKind` enum dispatched in one `is_capability_present`
function (rather than a per-capability `detect` callable), so the four detection shapes live
in one place. There is no `is_recommended` flag: whether a new port normally wants a capability
is prose in its `description`, not a boolean the code branches on.

`AGENT_CAPABILITIES` is the ordered list. `AgentClassInfo` bundles what detection and scope
need: the agent class, the agent-type/owner facts for the module-level kinds, and the kind
traits (`is_headless`, `is_cli_backed`) read from marker mixins.

### Class-level mixins to introduce

`HasStreamingSnapshotMixin`, `HasUnattendedModeMixin`, `HasPermissionPolicyMixin`,
`HasVersionManagementMixin`, `HasSessionPreservationMixin`, `HasSessionAdoptionMixin` -- each a
small ABC carrying the abstract method(s) that capability already implies (e.g.
`HasUnattendedModeMixin` declares the auto-allow application step that claude/agy/opencode/codex
each implement differently, and that pi implements degenerately; `HasSessionPreservationMixin`
declares the preserve step that all five `on_destroy` overrides already call; its read-side
counterpart `HasSessionAdoptionMixin` declares the `adopt_session` step that claude's
`on_after_provisioning` calls to resume `--adopt-session` / `--from` context). Unattended and
policy are deliberately *two* mixins, not one, so claude and pi can claim the first without the
second. The `Has…Mixin` shape matches the existing capability mixins (`HasTranscriptMixin`,
`HasCommonTranscriptMixin`). Contract-bearing, not bare markers: you cannot inherit one without
implementing its method, and the existing behavior is routed through it, so membership and
implementation are the same fact. The four existing transcript/headless mixins are folded into
the registry as-is. (Names are bikeable -- `HasUnattendedModeMixin` could be
`SupportsUnattendedRunMixin`; the suffix is fixed.)

`CliBackedAgentMixin` is a **kind marker**, not a capability -- it classifies the agent so scope
can be derived (see [Capability scope](#capability-scope-the-na-state)) and carries no matrix row
of its own. Every agent that wraps a specific external CLI (claude, codex, antigravity, opencode,
pi, and headless variants) inherits it; the bare `command` / `headless_command` runners do not, so
`is_cli_backed` is the positive trait that scopes the CLI-only rows. This is why `command` needs
no special class for *scoping* -- a minimal `CommandAgent` survives only to declare
`HasUnattendedModeMixin` (unattended by construction).

Live output unifies what used to be two rows. A TUI agent surfaces it as a streaming-snapshot
file (`HasStreamingSnapshotMixin`) and a headless agent as incremental stdout chunks
(`StreamingHeadlessAgentMixin`); both inherit a shared bare marker, `SupportsLiveOutputMixin`,
so "can stream live output before a turn completes" is one `live_output` capability regardless
of surface. `headless_output` (plain `HeadlessAgentMixin`) stays a separate row, scoped
`HEADLESS_ONLY`.

**Auto-install is a *base* capability, not one of the optional mixins above.** Checking the
binary is present and installing it if missing is cheap and every agent should have it, so it
belongs on the base (a method every plugin supplies its install command for, invoked by the
shared provision flow), and the agents currently missing it (antigravity, opencode, codex) get
it added rather than left as a gap. The one non-trivial part is per-CLI: each CLI's install
command differs (claude's installer script, pi's npm, codex's `codex update`/standalone, an
opencode install script, and agy's `curl -fsSL https://antigravity.google/cli/install.sh | bash`
-- which drops the `agy` binary into `~/.local/bin/`), so "toss it in" still means sourcing each
command. All real agents install through the shared `ensure_cli_installed` helper (claude no
longer carries a bespoke install block). Version management (pin vs auto-update) stays a
*distinguishing* capability (`HasVersionManagementMixin`: claude, codex), since not every CLI
exposes version control; it is a functional contract -- `reconcile_installed_version` enforces
the agent's version intent against the already-present binary (claude verifies its pin, codex
runs its update policy), not just a descriptive label.

## Discoverability: making "you should implement this" obvious

Detecting membership silently from pluggy gives no nudge to a new agent author, so
discoverability comes from two things, no extra machinery:

- **The generated matrix is itself the checklist.** A new port's column shows a gap cell for
  every capability it lacks, so the gaps are visible at a glance.
- **Each capability's `description` says whether a new port normally wants it** (e.g. "only
  needed if the agent runs under `mngr schedule`"), so the matrix gap reads as either "you
  should fill this" or "fine to skip" without a separate flag.

The capability list is cross-linked from the parity spec's New-CLI investigation checklist,
so the doc points at the code, not a copy. A `-` gap reads unambiguously as "absent, and
applicable" -- whether to fill it is answered by the capability's `description` -- while an
`n/a` cell signals the capability does not apply to that kind of agent, so it is not a gap at
all.

## The generated matrix and drift guard

A single function renders `AGENT_CAPABILITIES` x registered agents into a markdown matrix.
It is written to its **own generated doc**, not embedded in this spec -- generated content
does not belong inside a hand-authored `specs/` document. Its home is
`libs/mngr/docs/concepts/agent_capabilities.md`, alongside the existing `agent_types.md` /
`idle_detection.md` / `plugins.md`. `scripts/make_agent_capabilities_doc.py` regenerates it
(`just regenerate-agent-capabilities-doc`) and a drift-guard test
(`test_capability_matrix_doc_is_current`), plus the script's `--check` mode, fail if it is
stale. Unlike the CLI docs there is **no** pre-commit/pre-push hook regenerating it -- the
matrix changes rarely, so the CI drift-guard test is the safety net. The parity spec drops its
hand-maintained "Current state matrix" section and instead *links* to the generated doc; the
rich per-cell *mechanism* prose in the dimension sections stays in the parity spec.

## Driving the e2e harness from the registry (open follow-up)

The registry, the generated matrix, and its drift guard have shipped; this is the remaining
follow-up. The same registry that generates the matrix should also drive end-to-end coverage: for each
registered agent, the e2e harness **walks the capabilities that agent declares and exercises
each one** against a real running agent. The registry already knows agent x capability
membership, so it is the natural parametrization -- a new agent automatically gets walked
through every capability it claims, and a *declared-but-broken* capability (the mixin is
inherited / the hookimpl is registered, but the behavior does not actually work end-to-end)
is caught, which a pure `issubclass` check cannot see.

How it wires up, respecting the package/test split (exercise logic is test code and cannot
live in the shipped package):

- The capability *exercise* lives in the e2e/release test layer as a
  `{capability_key: exercise_fn}` map, where each `exercise_fn` takes a live agent handle and
  asserts the capability actually works (e.g. for `waiting_reason`, block a real agent on an
  approval prompt and assert the marker appears -- the shape the opencode release test already
  uses).
- The harness reads agent x capability membership from the registry and, per agent, runs only
  the exercises for the capabilities that agent declares. A capability degenerately implemented
  (pi's single-value `waiting_reason`) is exercised in its degenerate form -- a real check that
  the one value is produced, not skipped.
- A **coverage test** asserts every capability `key` in `AGENT_CAPABILITIES` has an
  `exercise_fn`, so a new capability cannot be added without e2e coverage (the registry is the
  forcing function). Capabilities whose exercise is impractical in CI (e.g. deploy
  contributions needing a real `mngr schedule` image) register an explicitly-`xfail`/skip
  exercise with a documented reason rather than being silently absent.

This makes the registry the single source behind three things at once: the generated matrix,
the drift guard, and e2e coverage -- so "what each agent can do" is declared once and both
documented and tested from that one declaration.

## What stays prose

Everything universal: registration skeleton, launch assembly, idle gating (incl.
subagents), readiness, input delivery, auth, config/HOME isolation, settings sync, resume,
mngr-owned start dialogs (interactive: pulled into mngr prompts; unattended: defaulted),
transcript *mechanism*, process name, workspace path quirks. These remain dimension sections
in the parity spec, because their content is how-not-whether.

## Non-goals / open questions

- Not refactoring module-level hookimpl capabilities onto the agent class (rejected in
  review as churn without honesty gain; pluggy detection is the source of truth for those).
- Resolved: the drift guard is a plain equality test (`test_capability_matrix_doc_is_current`),
  not a ratchet -- ratchets fit "a count that only decreases," which doesn't match an equality
  check.
- `offline_agent_field_generators` is unused by every plugin today; included as a detectable
  capability only if/when one implements it.
- **`n/a` is code-derived, never hand-declared.** The original design forbade `n/a` entirely;
  it was reintroduced only once the matrix covered kinds for which some capabilities are
  genuinely inapplicable (headless / bare-command), and only as a `scope` derived from
  code-detectable kind traits (see [Capability scope](#capability-scope-the-na-state)). The
  invariant that still holds: zero hand-kept per-cell data. *Instance*-level would-be-n/a
  (pi's missing approval gate) is still handled honestly via degenerate implementation, not a
  cell state.
- The opencode/codex permission-row question is resolved: both carry `HasPermissionPolicyMixin`
  (opencode a `permission` block via `config_overrides`; codex `sandbox_mode` / `approval_policy`),
  so both are an honest `Y`.
