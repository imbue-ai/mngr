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
runners -- some capabilities became genuinely inapplicable to a *whole kind*: a pane-scraping
streaming snapshot on a headless agent, or a CLI install / version / usage concern on a bare
shell command. Marking those `n/a` is more honest than `-` (which would imply "could have it,
just doesn't"), and because the kind is itself a code-detectable fact, it stays fully derived.

`n/a` is for *kind*-level inapplicability only. An *instance*-level gap is still handled
honestly without it: pi has no tool-approval gate, but rather than marking permissions `n/a`,
pi *implements* Unattended operation degenerately (auto-allow is always on; explicitly setting
it off is a hard error, since pi cannot honor a gate) and *implements* the `waiting_reason`
field with a single-value enum (a real extension point for if pi ever gains an approval gate).
What pi genuinely lacks -- a per-resource allow/deny/ask policy -- is an honest `-`.

### The capabilities

| Capability | Has it today | Wired today as |
|---|---|---|
| Raw transcript | all five | class mixin (`HasTranscriptMixin`) |
| Common transcript | all five | class mixin (`HasCommonTranscriptMixin`) |
| Session preservation on destroy | all five | `on_destroy` override + config flag |
| Headless output | (headless variants) | class mixin (`HeadlessAgentMixin`) |
| Streaming headless output | (headless variants) | class mixin (`StreamingHeadlessAgentMixin`) |
| Streaming snapshot (live TUI view) | claude | config + provisioned watcher script |
| Unattended operation (auto-allow in-run tool prompts) | all five (pi degenerately) | auto-allow config |
| Per-resource permission policy (allow/deny/ask) | antigravity, opencode, codex | config / `config_overrides` / `sandbox_mode` |
| Auto-install (install the binary if missing) | claude, pi | `check_installation` + installer/npm |
| Version management (pin vs auto-update) | claude, codex | claude `version` pin; codex `update_policy` |
| Deploy / scheduling contributions | claude | **pluggy hookimpl on the plugin module** |
| Field generators (`waiting_reason`) | claude, codex, opencode | **pluggy hookimpl on the plugin module** |
| Usage tracking (token/cost emission) | claude, opencode, pi, codex | **sibling `mngr_<harness>_usage` plugin (hookimpls)** |

The "Has it today" column was **corrected against the code** (this is
exactly the drift the matrix is meant to kill): the earlier draft wrongly listed claude under
per-resource policy (it is blanket-hooks only), opencode under install management (it has
none), and pi under `waiting_reason` (it has none yet -- pi's degenerate single-value generator
is still to be added). "Install/version management" was one lumped row hiding three independent
axes; it is now split into **auto-install** (get the binary present: claude, pi) and **version
management** (control which version runs -- claude pins against its own auto-update, codex
runs an update policy). The generated matrix is the source of truth; this table is illustrative.

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
| `CLI_BACKED_ONLY` | agents that wrap a specific CLI | transcripts, `auto_install`, `permission_policy`, `version_management`, `usage_tracking`, `session_resume` |
| `INTERACTIVE_ONLY` | CLI-backed and not headless | `waiting_reason_field` |
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
a class mixin can be *inherited* by a kind that the scope excludes (a CLI-only transcript mixin
could be inherited by a hypothetical command-runner subclass), so an out-of-scope class-mixin
hit renders `n/a` rather than erroring. The deliberately-registered kinds (a field generator
keyed by agent type, a usage source, a deploy hookimpl) cannot be inherited by accident, so an
out-of-scope hit *there* means the scope is wrong and rendering raises -- a drift guard.

## The capability registry

One module (`libs/mngr/imbue/mngr/agents/agent_capabilities.py`) declares the
capabilities and how to detect each. Sketch:

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
command. Version management (pin vs auto-update) stays a *distinguishing* capability
(`HasVersionManagementMixin`: claude, codex), since not every CLI exposes version control.

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
does not belong inside a hand-authored `specs/` document. The natural home is
`libs/mngr/docs/concepts/agent_capabilities.md`, alongside the existing `agent_types.md` /
`idle_detection.md` / `plugins.md`. A pre-commit step regenerates it (mirroring the existing
"Regenerate CLI markdown docs" hook) and a test fails if it is stale, so the file is always
current. The parity spec drops its hand-maintained "Current state matrix" section and instead
*links* to the generated doc; the rich per-cell *mechanism* prose in the dimension sections
stays in the parity spec.

## Driving the e2e harness from the registry

The same registry that generates the matrix should also drive end-to-end coverage: for each
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

## Implementation plan -- core phases done

1. **(done)** `agents/agent_capabilities.py`: `AgentCapability`, `AgentClassInfo`, the
   registry, and the matrix renderer, with class-mixin / field-generator / plugin-hookimpl /
   usage-source detection. Unit-tested.
2. **(done)** The live `build_agent_class_infos(plugin_manager)` builder, the generated matrix
   doc (`libs/mngr/docs/concepts/agent_capabilities.md`), and the drift-guard test
   (`just regenerate-agent-capabilities-doc`).
3. **(done)** The capability mixins, wired across the plugins, routing existing behavior
   through each contract method: streaming snapshot -> claude(+subtypes); session preservation
   -> all five; unattended operation -> all five (pi degenerate, rejects explicit-off);
   per-resource policy -> agy/opencode/codex; version management -> claude/codex; pi's
   single-value `waiting_reason`; module-level `deploy_contributions` + `usage_tracking`
   (detected by owning-plugin entry-point name); and **auto-install** as a base capability (`HasAutoInstallMixin`
   + shared `ensure_cli_installed`) added to agy/opencode/codex, pi routed through the helper,
   claude keeping its version-aware flow. One changelog entry per touched project.
4. **(done)** Matrix presentation and the `n/a` state: a fixed column order with the bare
   command runners last (and a guard that raises if a registered agent type is neither ordered
   nor explicitly excluded, so none is silently dropped); the `headless_output` row pinned last;
   and the code-derived `scope` model with its `CapabilityScope` enum and the positive
   `CliBackedAgentMixin` kind marker, so `raw`/`common` transcript, install, version, usage,
   permission, and `session_resume` are `n/a` for the bare command runners, `waiting_reason` is
   `n/a` for headless/command, and `headless_output` is `n/a` off the headless agents. The TUI
   streaming snapshot and headless incremental output were unified into one `live_output` row via
   a shared `SupportsLiveOutputMixin` marker. `session_resume` (claude's `--adopt-session` /
   `--from` carry-forward) was added via `HasSessionAdoptionMixin`. `GenericCommandAgentMixin` was
   removed in favour of the positive marker; a minimal `CommandAgent` survives only to declare
   unattended-by-construction. Also wired `is_unattended_enabled()` through each agent's
   auto-allow apply-site so the contract method is the single source of truth (behavior-preserving).
5. **(follow-up PR)** The registry-driven **release** harness: walk each agent's declared
   capabilities and run a real `exercise_fn` per capability against a live agent (the
   behavioral check detection cannot give). Detection itself is already covered in CI by the
   drift guard + the builder integration test, so a CI-cheap "coverage forcing" test over prose
   pointers was deliberately *not* shipped -- it would force a sentence, not a test. The real
   forcing function (a capability cannot ship without an `exercise_fn`) lands with the harness.

Verified opencode and agy auto-install end-to-end on real Modal hosts (which ship without the
CLIs): both printed "<cli> installed successfully" during provision.

The matrix was **corrected against the code** throughout (the drift this system kills): per-
resource policy is agy/opencode/codex (not claude -- blanket hooks only); field generators are
claude/codex/opencode (+ pi's degenerate one); install split into auto-install (all real
agents) and version management (claude/codex); `mngr_kanpan` is not an agent type so it is no
matrix column.

## Non-goals / open questions

- Not refactoring module-level hookimpl capabilities onto the agent class (rejected in
  review as churn without honesty gain; pluggy detection is the source of truth for those).
- Whether the drift guard is a plain test or a ratchet -- ratchets fit "a count that only
  decreases," which doesn't match an equality check, so a plain test is the likely fit.
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
