# Agent Usage Plugins

## Purpose and scope

`mngr usage` today reports rolling-window cost and rate-limit data for **Claude**
agents only, because the only writer that exists is `mngr_claude_usage`. This
spec describes how to extend usage tracking to the other agent harnesses:
**OpenCode**, **pi**, and **Codex**. Antigravity (`agy`) and the
Claude-subagent-proxy are explicitly out of scope (see
[Out of scope](#out-of-scope-and-deferred-work)).

The work is organized as three layers:

1. **Generalize the `mngr_usage` event schema and reader** so usage can be
   reported as raw token counts (not only pre-computed dollars), with the reader
   deriving and provenance-flagging cost.
2. **A canonical token-pricing table** (with an accuracy guard against
   `litellm`'s numbers) so the reader can turn tokens into dollars, shared with
   `apps/modal_litellm` by a drift test rather than a runtime import.
3. **Three thin per-harness writer plugins** that each emit only the usage data
   their underlying tool natively exposes.

Audience: developers implementing the usage plugins. Read alongside the
`imbue-mngr-usage` README (`libs/mngr_usage/README.md`), the
`imbue-mngr-claude-usage` README (`libs/mngr_claude_usage/README.md`), and the
[agent-plugin-parity spec](../agent-plugin-parity/spec.md).

## Background: how `mngr usage` works today

The design deliberately splits into a generic reader and per-harness writers:

- **`mngr_usage` is the agent-agnostic reader.** It walks
  `<agent_state_dir>/events/<source>/usage/events.jsonl` under every agent
  (live and destroy-time-preserved), aggregates per `<source>`, and renders
  human / JSON / jsonl / format-template output. It knows nothing about any
  specific harness. Adding a harness means adding a writer; the reader is
  unchanged in principle.
- **A writer is responsible for appending `cost_snapshot` events** at the
  conventional path. `mngr_claude_usage` is the reference writer: a statusline
  shim that reshapes Claude Code's statusline payload into one event per render.

The Claude writer is trivial because Claude Code's statusline payload hands over
both halves of the data already shaped: `cost.total_cost_usd` (always present)
and `rate_limits` (the 5h / 7d windows, subscription only). No arithmetic.

### Current event contract

Each JSONL line carries `session_id` (required, non-empty) plus **at least one
of** `rate_limits` or `cost`:

```jsonl
{"source":"claude/usage","type":"cost_snapshot","event_id":"evt-<hex>",
 "timestamp":"<ISO 8601>","session_id":"<uuid>",
 "cost":{"total_cost_usd":<float>,...},
 "rate_limits":{"<window>":{"used_percentage":<float>,"resets_at":<unix>,...}}}
```

### Current reader aggregation semantics (important)

These shape the design, see `libs/mngr_usage/imbue/mngr_usage/data_types.py`:

- **Cost is treated as a cumulative-per-session reading.** Each event's
  `cost.total_cost_usd` is the running total for that session so far. The reader
  takes the **freshest** event per `session_id`, then computes each session's
  **own contribution** as the delta from the prior session's cumulative reading
  *within the same harness process* (`SessionCostRecord.cost`). This is what
  lets `/clear` rotate `session_id` inside one cumulative-cost process without
  double-counting. Summing contributions across all sessions in a recency
  window recovers true spend.
- **Rate-limit windows reduce freshest-wins** per source (an account-level
  counter).
- **Cost is split by `CostMode`** (`SUBSCRIPTION` vs `API_KEY`), currently
  inferred from the **presence of `rate_limits`** in the session's events
  (`rate_limits` is emitted only under a Claude.ai subscription). The two are
  never lumped: `subscription_cost` is imputed value, `api_cost` is real
  billable spend.

The cumulative-reading assumption and the rate-limits-based mode inference are
both Claude-specific and must be generalized (see below).

## Goals and non-goals

**Goals**

- Track per-session cost for OpenCode, pi, and Codex agents, surfaced through
  the existing `mngr usage` / `mngr usage wait` interfaces with no new
  user-facing commands.
- Let a writer report **raw token counts + model** when it does not have a
  dollar figure, and have the reader derive cost centrally.
- Keep dollars as the cross-harness comparable unit so existing predicates such
  as `api_cost.total_cost_usd > 20` work across a mixed fleet.
- Keep writers thin and host-safe (shell / in-process TS, no heavy deps),
  matching the shell-only `mngr_claude_usage`.

**Non-goals**

- Rate-limit / quota windows for the new harnesses. OpenCode, pi, and Codex
  (API-key mode) do not expose Claude-style windows; this spec is cost-focused.
  The window schema stays optional and untouched; a harness that later exposes
  quota data can populate it without a schema change.
- Changing how Claude usage works. Claude continues to report cost directly; the
  generalization is purely additive.
- Antigravity usage (deferred), and any new `mngr usage` CLI surface.

## Design principles

1. **Writers emit only what their tool natively exposes.** Do not synthesize a
   dollar figure on the agent host. OpenCode reports cost; pi and Codex report
   tokens; each writer emits its native shape.
2. **The reader normalizes to dollars.** Cost derivation from tokens happens in
   one place — the reader — which runs where `mngr usage` is invoked (full
   Python env), not on remote agent hosts.
3. **Prefer harness-reported cost over our estimate.** When an event carries
   both a reported `cost` and `tokens`, the reader uses the reported cost and
   tags it `REPORTED`. It estimates from tokens only as a fallback.
4. **Tokens are first-class, not a private implementation detail.** Token sums
   are surfaced in the JSON / CEL context so token-native predicates are also
   possible and so derived dollars are auditable.
5. **Cost provenance is explicit.** Every dollar figure is tagged `REPORTED`
   (harness gave it) or `ESTIMATED` (reader derived it from tokens). Estimates
   and reported numbers are never silently blended.

## Layer 1: schema and reader generalization

### Wire schema additions

Add two optional fields to the event line and relax the contract:

- `tokens` (object, optional): `{input, output, cache_read, cache_creation}`,
  all optional integers. **Cumulative-per-session** counts (see
  [Cumulative discipline](#cumulative-discipline)).
- `model` (string, optional): the model id the tokens were billed against.
  Required for cost to be derivable when `cost` is absent.
- `cost_mode` (string, optional): writer-declared `"SUBSCRIPTION"` or
  `"API_KEY"`. Lets a harness that does not emit `rate_limits` still declare
  whether its cost is imputed or real. When absent, the reader falls back to the
  current rate-limits inference, then defaults to `API_KEY`.

New contract: an event carries `session_id` plus **at least one of**
`rate_limits`, `cost`, or `tokens`. An event with `tokens` but no `model` is
accepted (tokens still aggregate) but cannot contribute a derived dollar figure;
the reader logs a WARNING naming the source.

### Data model additions (`data_types.py`)

- `TokenSnapshot(FrozenModel)`: `input`, `output`, `cache_read`,
  `cache_creation` (`int | None` each). Field-wise summable, mirroring
  `CostSnapshot`'s `_sum_optional` aggregation.
- `CostProvenance(UpperCaseStrEnum)`: `REPORTED`, `ESTIMATED`.
- `SessionCostRecord` gains: `tokens: TokenSnapshot | None`,
  `model: str | None`, `cost_provenance: CostProvenance`.
- `UsageSnapshot` gains token aggregates (`subscription_tokens`, `api_tokens`,
  or a single `tokens` aggregate split by mode mirroring the cost split) and an
  `is_cost_estimated` view (true if any contributing session in that mode is
  `ESTIMATED`), so consumers can tell a mode's dollar total is partly or wholly
  derived.

`CostMode`'s docstring is generalized: the `SUBSCRIPTION`/`API_KEY` distinction
is now "imputed vs. real billable", determined by the writer-declared
`cost_mode` hint first, then rate-limits inference, then default. `CostMode`
(who pays / billable) and `CostProvenance` (how the number was obtained) are
**orthogonal axes**:

| Harness            | Typical `CostMode` | Typical `CostProvenance` |
| ------------------ | ------------------ | ------------------------ |
| Claude (sub)       | `SUBSCRIPTION`     | `REPORTED`               |
| Claude (API key)   | `API_KEY`          | `REPORTED`               |
| OpenCode           | `API_KEY`          | `REPORTED`               |
| pi (API key)       | `API_KEY`          | `ESTIMATED`              |
| Codex (API key)    | `API_KEY`          | `ESTIMATED`              |

### Reader cost resolution

When building a `SessionCostRecord` for a session, per session (freshest event):

1. If `cost.total_cost_usd` is present → use it, `cost_provenance = REPORTED`.
2. Else if `tokens` and `model` are present and `model` is in the pricing table
   → `total_cost_usd = compute_cost(model, tokens)`,
   `cost_provenance = ESTIMATED`.
3. Else → `total_cost_usd = None` (the record still carries `tokens`);
   `cost_provenance = ESTIMATED`; log a WARNING if `tokens` present but `model`
   unknown/missing.

Tokens are always carried through (steps 1-3) when present, independent of which
branch produced the dollar figure. Mode is resolved as described above.

### Cumulative discipline

The reader's per-session delta machinery assumes each event is a
**cumulative-to-date** reading for its `session_id`. To reuse it unchanged, the
**writer contract requires cumulative-per-session readings** for both `cost` and
`tokens`. This is naturally satisfied:

- **Codex** `token_count` events already report cumulative session token usage.
- **OpenCode** and **pi** writers run **in-process** and see every message, so
  they keep a running per-session total in memory and emit the cumulative value.
  (Emitting per-message increments and summing in the reader is the alternative;
  it is rejected because it diverges from the existing cost path and breaks the
  `/clear`-resilient delta logic.)

**Note:** This means the cumulative-vs-incremental choice is pushed to the
writer, where the in-process harnesses can satisfy it cheaply, keeping the
reader's aggregation identical across all harnesses.

## Layer 2: canonical pricing table

### Table

A new module in `mngr_usage` (e.g. `pricing.py`) holds
`MODEL_PRICING: dict[str, PerTokenPrices]` keyed by model id, using **litellm's
field names** so entries are directly comparable to `apps/modal_litellm`:
`input_cost_per_token`, `output_cost_per_token`,
`cache_creation_input_token_cost`, `cache_read_input_token_cost`. A
`compute_cost(model, tokens) -> float | None` helper does the arithmetic
(`input*p_in + output*p_out + cache_read*p_cr + cache_creation*p_cw`), returning
`None` for an unknown model.

The numbers are **human-curated from litellm**, not read from litellm at
runtime — mirroring the established posture in
`apps/modal_litellm/app.py` (which registers pricing inline precisely so cost
tracking stays correct on litellm versions whose bundled map predates a model).

**Model id normalization.** pi is multi-provider, so it emits provider-qualified
model ids. The table and `compute_cost` define one canonical key form (e.g.
`provider/model` lowercased) and the writers/reader normalize to it. An
unrecognized id resolves to `None` cost (never `$0`).

### Accuracy guard

A test asserts each curated entry matches `litellm`'s bundled
`model_prices_and_context_window` map, with an explicit allow-list for
intentional divergences (e.g. legacy Opus tiers priced higher than a stale
bundled map). When litellm disagrees outside the allow-list, the test fails so a
human reviews the change rather than silently trusting either side.

**Warning:** Unknown / brand-new models must surface as `None` cost plus a
WARNING, never as `$0`. A silent `$0` reads as "free", the exact failure mode
the modal_litellm comment guards against.

### Sharing with `apps/modal_litellm`

Share by **drift test, not runtime import** — the same mechanism the repo
already uses to keep `apps/modal_litellm/app.py` and
`litellm_proxy/config.yaml` byte-for-byte identical (`config_drift_test.py`):

- `modal_litellm` keeps its inline Anthropic pricing verbatim (its deploy image
  stays thin; no `libs/` dependency is pulled into the Modal image, and the
  static `config.yaml` mirror is unaffected).
- A new test asserts `modal_litellm`'s Anthropic entries equal the canonical
  table's Anthropic entries. The canonical table is the single source of truth;
  the test makes drift impossible without changing the import graph.

This is a follow-up that must **not block** Layers 1 and 3: build the canonical
table inside the usage work first (designed from day one to be the shared
source), ship the writers, then add the cross-drift test in a separate PR.

## Layer 3: per-harness writer plugins

Each writer is a new package `libs/mngr_<harness>_usage` mirroring
`mngr_claude_usage`'s structure (pyproject, README, changelog, an
`on_before_provisioning` hookimpl gated on `isinstance(agent, <Harness>Agent)`,
resources, tests). All file I/O goes through `host.*` so it works for local and
remote agents.

Crucially, **the injection point is not the hard part** — every harness already
has a per-turn hook and a transcript writer wired and tested. The differentiator
is what usage data the tool exposes.

### OpenCode (easy: reported cost, no new logic)

- **Injection:** the existing in-process TypeScript plugin
  (`libs/mngr_opencode/imbue/mngr_opencode/resources/mngr_opencode_plugin.ts`)
  already holds the assistant `message` object and writes a common-transcript
  record with `usage: null` (plugin.ts:236).
- **Data:** OpenCode computes `message.cost` and `message.tokens` itself. The
  writer emits a cumulative-per-session `cost` (and `tokens` for auditability)
  per message. `cost_mode = API_KEY`, provenance `REPORTED`. No pricing math.
- **To verify:** that `message.cost` / `message.tokens` are actually populated
  in the pinned OpenCode version (see [Facts to verify](#facts-to-verify)).

### pi (moderate: token-derived cost, multi-provider)

- **Injection:** the existing TypeScript lifecycle extension
  (`libs/mngr_pi_coding/imbue/mngr_pi_coding/resources/mngr_pi_lifecycle.ts`)
  already extracts `usage.{input,output,cacheRead,cacheWrite}` per assistant
  message (lifecycle.ts ~line 401).
- **Data:** emit cumulative-per-session `tokens` + `model` (provider-qualified).
  No reported cost → reader estimates. `cost_mode = API_KEY` (subscription
  variants, if any, declare `SUBSCRIPTION`), provenance `ESTIMATED`.
- **Added logic:** model-id normalization across providers
  (Anthropic/OpenAI/Gemini/Groq/OpenRouter) so pricing lookups resolve. Map
  pi's `cacheRead`/`cacheWrite` to `cache_read`/`cache_creation`.

### Codex (moderate: token-derived cost, awkward injection)

- **Injection:** no statusline; Codex has shell lifecycle hooks plus a polling
  transcript streamer over the rollout JSONL
  (`libs/mngr_codex/imbue/mngr_codex/resources/{common,stream}_transcript.sh`).
  The rollout already contains `token_count` events. The usage writer piggybacks
  on that rollout-reading path rather than a clean hook — more awkward than the
  TS harnesses.
- **Data:** emit cumulative-per-session `tokens` + `model` from the rollout's
  `token_count` events. `cost_mode = API_KEY`, provenance `ESTIMATED`.
- **To verify:** the exact `token_count` field names, that it is cumulative, and
  whether ChatGPT-subscription mode also exposes rate-limit windows there (if
  so, Codex could additionally populate `rate_limits` and get Claude-style
  windows — a bonus, not required).

### Difficulty summary

| Harness   | Injection point (exists)        | Native data        | Cost path           | Difficulty |
| --------- | ------------------------------- | ------------------ | ------------------- | ---------- |
| OpenCode  | in-process TS plugin            | cost + tokens      | reported            | Easy       |
| pi        | in-process TS extension         | tokens (multi-prov)| estimated           | Moderate   |
| Codex     | rollout transcript streamer     | tokens             | estimated           | Moderate   |

## Edge cases and failure modes

- **Unknown / new model:** `compute_cost` returns `None`; the record keeps its
  tokens, `total_cost_usd` is `None`, provenance `ESTIMATED`, and a WARNING is
  logged. Never `$0`. The accuracy test surfaces models present in litellm but
  missing from the table.
- **Cache-token pricing nuance:** cache creation and cache read have different
  multipliers (and differ by provider). The table must carry both
  `cache_creation_input_token_cost` and `cache_read_input_token_cost`; the
  writers must map their native cache fields onto the right one.
- **Mixed provenance in one mode aggregate:** a mode's dollar total may sum
  reported and estimated sessions. `is_cost_estimated` flags that so a consumer
  can distinguish a precise total from a partly-derived one.
- **Reported + tokens both present (OpenCode):** reported cost wins for the
  dollar figure (per design principle 3); tokens are still carried for
  auditability and token predicates.
- **Per-message vs cumulative drift:** if an in-process writer fails to
  accumulate and emits increments, the reader's delta logic under-counts.
  Writers MUST emit cumulative-per-session; covered by writer unit tests.
- **Missing `model` with tokens:** accepted, tokens aggregate, no dollar
  estimate, WARNING logged (mirrors the existing "missing session_id" warning
  pattern in the README).
- **Multi-account ambiguity:** the existing `claude`-source caveat (multiple
  accounts sharing one source) applies equally to the new sources; unchanged.

## Testing strategy

- **Unit (`*_test.py`):** `compute_cost` arithmetic incl. cache tokens;
  unknown-model → `None` + warning; provenance selection (reported preferred
  over estimated); `cost_mode` resolution precedence (hint → rate-limits →
  default); `TokenSnapshot` aggregation; model-id normalization.
- **Pricing accuracy test:** curated table vs litellm bundled map with
  allow-list.
- **modal_litellm cross-drift test:** Anthropic subset equality (Layer 2
  follow-up PR).
- **Integration:** each writer, given a representative harness payload / rollout
  line, appends a well-formed cumulative event; the reader produces the expected
  per-session cost and tokens.
- **Release (`test_*.py`, `@pytest.mark.release`):** end-to-end per harness —
  provision an agent, drive a turn, assert `mngr usage` reflects its cost.
  Follow the existing `mngr_claude_usage` test layout.

## Facts to verify

These were established as unverified during design and gate the difficulty
claims; confirm before implementing the affected writer:

1. **OpenCode:** `message.cost` and `message.tokens` are populated in the pinned
   OpenCode version (decides OpenCode-as-easy).
2. **Codex:** `token_count` rollout event field names; that the values are
   cumulative; and whether subscription-mode rate-limit windows are present
   there.
3. **pi:** the auth-mode → `cost_mode` mapping, and the exact provider-qualified
   model-id format pi emits (decides the normalization map).

## Out of scope and deferred work

- **Antigravity (`agy`):** deferred. Post-`agy-statusline`, antigravity has a
  statusline injection point, but its payload carries only
  `agent_state`, `conversation_id`, `model`, `context_window` — **no cost,
  tokens, or rate_limits**. There is no usage data to write without a new
  upstream source (parsing agy session files, if they even persist token
  counts). This was already deferred once as `mngr_gemini_usage` in the
  gemini-feature-parity work. It needs an investigation spike before it is
  schedulable.
- **Claude-subagent-proxy:** out of scope. It is a plugin on top of Claude, not
  an independent harness; its subagents are Claude Code processes already
  covered by `mngr_claude_usage`.

## Implementation order

1. **Layer 1** — schema + reader generalization (tokens, derive-and-flag cost,
   provenance, cumulative discipline, generalized `cost_mode`). Land with the
   canonical pricing table module (Layer 2 table only).
2. **OpenCode writer** — proves the non-Claude path end-to-end with no new cost
   logic (reported cost).
3. **pi writer** — first token-derived harness; exercises normalization.
4. **Codex writer** — token-derived via the rollout streamer.
5. **modal_litellm cross-drift test** — separate PR; does not block the above.
6. **Antigravity spike** — separate investigation; build-vs-defer decision.

## Base branch note

This work is developed on `mngr/agents-usage`, whose base is
`mngr/unify-ts-plugins` **plus** `mngr/agy-statusline` (merged in locally). The
PR should target a synthetic base branch that is exactly those two merged
together, so the antigravity statusline groundwork is treated as part of the
base rather than as a change introduced by this work.
