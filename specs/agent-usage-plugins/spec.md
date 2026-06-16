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
  specific harness. Today its aggregation is a single Claude-shaped reader; this
  spec moves per-source aggregation behind a hook (see
  [Reader architecture](#reader-architecture-a-hook-plus-shared-utils)), so
  adding a harness means adding a writer plus a thin reader hookimpl.
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

- Rate-limit / quota windows are not the focus. OpenCode and pi (API-key mode)
  do not expose Claude-style windows, so for them this spec is cost-only. The
  window schema stays optional; a harness that exposes quota data can populate
  it without a schema change. **Exception:** Codex's `token_count` events *do*
  carry rate-limit windows in subscription mode (verified — see
  [Verified harness facts](#verified-harness-facts)), so the Codex writer should
  populate `rate_limits` as a low-cost bonus; this is the one new harness that
  gets windows.
- Changing how Claude usage works. Claude continues to report cost directly; the
  generalization is purely additive.
- Antigravity usage (deferred), and any new `mngr usage` CLI surface.

## Design principles

1. **Writers emit only what their tool natively exposes.** Do not synthesize a
   dollar figure on the agent host. OpenCode and pi report cost (pi falls back to
   tokens where it has none); Codex reports tokens; each writer emits its native
   shape.
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

### Reader architecture: a hook plus shared utils

Aggregation semantics are inherently harness-specific (Claude needs
process-cumulative deltas; the others are session-cumulative — see
[Cumulative scope](#cumulative-scope-process-vs-session)), so rather than let
`mngr_usage` accrete per-harness branching, **per-source aggregation moves
behind a reader hook**, with the reusable machinery shipped as shared utils that
the current harnesses simply call:

- **`mngr_usage` owns** the data types (below), `compute_cost` + the pricing
  table, source **discovery / destroy-time preservation / dispatch / rendering**,
  a **reader hookspec**, and reusable aggregation utils:
  - `aggregate_process_cumulative(events_by_agent, ...) -> UsageSnapshot` — the
    existing Claude delta-and-process-boundary logic, lifted verbatim into a
    named util (no behavior change, existing tests keep covering it).
  - `aggregate_session_cumulative(events_by_agent, ...) -> UsageSnapshot` —
    freshest-reading-per-`session_id` (each session is its own counter), with
    cost derived/flagged per [Reader cost resolution](#reader-cost-resolution).
- **Each usage plugin** implements a thin `aggregate_usage_source` hookimpl that
  claims its `source_name` and calls the matching util. `mngr_claude_usage`'s
  hookimpl calls `aggregate_process_cumulative`; codex/opencode/pi call
  `aggregate_session_cumulative`. A future exotic harness can write fully bespoke
  aggregation in its own hookimpl without touching `mngr_usage`.
- **Dispatch:** `mngr_usage` invokes the hook per discovered source (firstresult:
  the first plugin that claims the source wins). A source no plugin claims falls
  back to `aggregate_process_cumulative` for back-compat.

`mngr_usage` thus stays source-agnostic — it dispatches and renders; it never
hardcodes harness knowledge. The data types, `UsageSnapshot` shape, and pricing
helper are the shared contract every reader produces and consumes.

### Wire schema additions

Add two optional fields to the event line and relax the contract:

- `tokens` (object, optional): `{input, output, cache_read, cache_creation}`,
  all optional integers. **Cumulative-per-session** counts (see
  [Cumulative discipline](#cumulative-discipline)). **Wire convention:** `input`
  is the **non-cached** input count, and `cache_read` / `cache_creation` are
  separate additive buckets, so the cost is exactly
  `input·p_in + cache_read·p_cr + cache_creation·p_cw + output·p_out` with no
  double-counting. Writers MUST normalize to this even when their source reports
  `input` inclusive of cache (Codex does — its `input_tokens` includes
  `cached_input_tokens`, so the writer emits `input = input_tokens −
  cached_input_tokens`). `output` includes reasoning tokens (billed at the output
  rate). Providers without a cache-creation surcharge (OpenAI/Codex) simply omit
  `cache_creation`.
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
| pi (API key)       | `API_KEY`          | `REPORTED` (est. fallback) |
| Codex (API key)    | `API_KEY`          | `ESTIMATED`              |
| Codex (ChatGPT)    | `SUBSCRIPTION`     | `ESTIMATED`              |

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

### Cumulative scope: process vs session

Writers emit **cumulative-to-date** readings (cost and/or tokens), never
per-message increments — events from the same `session_id` carry a growing
total, and the reader takes the freshest. But there are **two cumulative
scopes**, which is why the aggregation util differs per harness:

- **Process-cumulative (Claude only).** Claude Code's cost is one counter that
  spans multiple `session_id`s — a `/clear` rotates the session id *without*
  resetting cost. So `aggregate_process_cumulative` partitions an agent's stream
  into processes (via cost-drop detection) and computes each session's
  contribution as a **delta from the prior session's reading** within the
  process. Taking freshest-per-session and summing would double-count, because a
  later session's reading already includes earlier ones.
- **Session-cumulative (Codex, OpenCode, pi).** Each `session_id` is its **own**
  counter that starts at zero; sessions do not share a running total. So
  `aggregate_session_cumulative` simply takes the freshest reading per
  `session_id` as that session's whole contribution — **no cross-session delta**.
  Applying Claude's delta logic here would be *wrong* (it would treat session 2's
  total as a delta from session 1).

  - **Codex** `token_count.info.total_token_usage` is cumulative for the session.
  - **OpenCode** and **pi** writers run **in-process**, see every message, and
    keep a running per-session total, emitting the cumulative value each event.

This distinction is the whole reason aggregation is a per-plugin util choice
rather than central logic: a plugin picks the scope that matches its harness.

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
- A drift test asserts every Anthropic model `modal_litellm` prices also exists
  in the canonical table with identical per-token prices (the canonical table may
  carry additional models). Changing a price on either side without the other
  fails the test, making the "mirrored verbatim" claim enforceable without
  changing the import graph.

**Implemented:** `apps/modal_litellm/mngr_usage_pricing_drift_test.py` loads
`app.py`'s `LITELLM_CONFIG` (the same way `config_drift_test.py` does) and
compares against `mngr_usage`'s `MODEL_PRICING`.

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
- **Data (verified, SDK 1.16.2):** every `AssistantMessage` carries
  **non-optional** `cost: number` and `tokens: {input, output, reasoning,
  cache: {read, write}}`, plus `modelID` / `providerID`
  (`@opencode-ai/sdk/.../types.gen.d.ts`). The writer accumulates these
  per-session (cost is per-message) and emits cumulative `cost` (provenance
  `REPORTED`, no pricing math) plus `tokens` for auditability. Map
  `cache.read → cache_read`, `cache.write → cache_creation`, fold `reasoning`
  into `output`. `cost_mode = API_KEY`.

### pi (easy: reports cost natively; estimate as fallback)

- **Injection:** the existing TypeScript lifecycle extension
  (`libs/mngr_pi_coding/imbue/mngr_pi_coding/resources/mngr_pi_lifecycle.ts`)
  already extracts `usage.{input,output,cacheRead,cacheWrite}` per assistant
  message (lifecycle.ts ~line 401).
- **Data (verified live, pi 0.79.1 — see [Verified harness facts](#verified-harness-facts)):**
  pi's native `assistant.usage` **includes a `cost` object** —
  `{input, output, cacheRead, cacheWrite, total}` — that pi computes
  client-side, with `total` matching the canonical Anthropic per-token prices to
  the digit. So pi is **reported-cost** (provenance `REPORTED`), not estimated:
  the writer emits `cost.total_cost_usd = usage.cost.total`. `lifecycle.ts`
  currently drops this — it must surface `usage.cost.total`. Token buckets are
  **non-overlapping** (`totalTokens = input + output + cacheRead + cacheWrite`),
  so `input` is already cache-exclusive — no subtraction needed (unlike Codex).
  Map `cacheRead → cache_read`, `cacheWrite → cache_creation`; emit `tokens` for
  auditability. The model is a **bare name** (`claude-opus-4-8`) with `provider`
  a **separate** field (`anthropic`). As shipped, the writer emits `cost_mode =
  API_KEY` unconditionally (pi runs against a real provider key); deriving the
  mode from `auth.json[provider].type` (`"api_key"` → `API_KEY`; oauth-style →
  `SUBSCRIPTION`) is a deferred refinement for oauth/subscription providers.
- **Fallback logic:** for a provider/model where pi does *not* compute a cost
  (`usage.cost` absent — possible for some of openai/gemini/groq/openrouter), the
  writer omits `cost` and the reader estimates from `tokens` + `provider/model`.
  Only that path needs the pricing table, so the writer must emit **both**
  `provider` and `model` (`lifecycle.ts` emits only `model` today — add
  `provider`) to form the canonical `provider/model` key.

### Codex (moderate: token-derived cost, awkward injection)

- **Injection:** no statusline; Codex has shell lifecycle hooks plus a polling
  transcript streamer over the rollout JSONL
  (`libs/mngr_codex/imbue/mngr_codex/resources/{common,stream}_transcript.sh`).
  The rollout already contains `token_count` events. The usage writer piggybacks
  on that rollout-reading path rather than a clean hook — more awkward than the
  TS harnesses.
- **Data (verified, codex 0.138.0):** the `token_count` event
  (`payload.type == "token_count"`) carries `info.total_token_usage`
  (**cumulative**: `input_tokens`, `cached_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`, `total_tokens`) and `info.last_token_usage` (the
  per-turn delta); `total = input + output`, `input_tokens` **includes**
  `cached_input_tokens`, and reasoning is a subset of output. The writer emits
  `input = input_tokens − cached_input_tokens`, `cache_read = cached_input_tokens`,
  `output = output_tokens` (no `cache_creation` — OpenAI has no cache-write
  surcharge). `cost_mode` is `SUBSCRIPTION` when `rate_limits`/`credits` are
  present (ChatGPT plan), else `API_KEY`; provenance `ESTIMATED`. The `model`
  comes from the rollout's `turn_context` / `session_meta`.
- **Bonus (verified):** the same event carries `rate_limits` —
  `primary` (`window_minutes: 300` = 5h) and `secondary` (`window_minutes: 10080`
  = 7d), each `{used_percent, resets_at}`, plus `credits`/`plan_type`. The writer
  maps these onto the `rate_limits` window schema (`used_percent → used_percentage`,
  `window_minutes·60 → window_seconds`), giving Codex subscription agents
  Claude-style windows.

### Difficulty summary

| Harness   | Injection point (exists)        | Native data            | Cost path  | Windows | Difficulty |
| --------- | ------------------------------- | ---------------------- | ---------- | ------- | ---------- |
| OpenCode  | in-process TS plugin            | cost + tokens          | reported            | no        | Easy     |
| pi        | in-process TS extension         | cost + tokens          | reported (est. fallback) | no   | Easy     |
| Codex     | rollout transcript streamer     | tokens + rate_limits   | estimated           | yes (sub) | Moderate |

## Install-wizard recommendation

`mngr plugin install-wizard` (and `mngr extras -i`) surface these plugins in their
two-phase picker via `PLUGIN_CATALOG`:

- **Phase 1** recommends the base `imbue-mngr-usage` plugin (the agent-agnostic
  `mngr usage` reader/CLI) for everyone -- it has no binary signal, so it is always
  pre-checked.
- **Phase 2** offers each per-harness provider (`imbue-mngr-<harness>-usage`) only
  when both its agent plugin and the base usage plugin are *present* -- already
  installed or selected in phase 1 -- expressed by the catalog entry's
  `requires_packages` gate, and pre-checks it when shown.

Antigravity has no usage provider (see
[Out of scope](#out-of-scope-and-deferred-work)), so the wizard offers a per-agent
usage extra for every agent type **except** antigravity. If an
`imbue-mngr-antigravity-usage` package is ever built, adding its catalog entry
(gated on `imbue-mngr-antigravity` + `imbue-mngr-usage`) is all the wizard needs.

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
- **modal_litellm cross-drift test:** Anthropic price equality (implemented:
  `apps/modal_litellm/mngr_usage_pricing_drift_test.py`).
- **Integration:** each writer, given a representative harness payload / rollout
  line, appends a well-formed cumulative event; the reader produces the expected
  per-session cost and tokens.
- **Release (`test_*.py`, `@pytest.mark.release`):** end-to-end per harness —
  provision an agent, drive a turn, assert `mngr usage` reflects its cost.
  Follow the existing `mngr_claude_usage` test layout.

## Verified harness facts

The difficulty claims were verified against the locally installed harnesses and
their real on-disk data / shipped schemas (OpenCode 1.16.2, Codex 0.138.0, pi
0.79.1). Results are folded into the per-harness sections above; in summary:

1. **OpenCode — confirmed (decides OpenCode-as-easy).** `AssistantMessage.cost`
   (`number`) and `.tokens` (`{input, output, reasoning, cache:{read, write}}`)
   are **non-optional** in the shipped SDK
   (`@opencode-ai/sdk/dist/gen/types.gen.d.ts`), with `modelID`/`providerID`.
   Reported cost, no pricing math.
2. **Codex — confirmed and better than assumed.** `token_count` payloads carry
   `info.total_token_usage` (cumulative) + `info.last_token_usage` (delta) with
   `input_tokens`/`cached_input_tokens`/`output_tokens`/`reasoning_output_tokens`/
   `total_tokens` (`input` is inclusive of cached; `total = input + output`), and
   **also carry `rate_limits`** (`primary` 5h, `secondary` 7d) in subscription
   mode — so Codex gets Claude-style windows as a bonus.
3. **pi — confirmed via a live mngr session (better than assumed).** Drove a
   two-turn `pi-coding` agent and read the raw session + common transcript. pi's
   native `assistant.usage` **includes a `cost` object**
   (`{input, output, cacheRead, cacheWrite, total}`) it computes client-side, so
   pi is **reported-cost**, not estimated. The residual is resolved: token
   buckets are non-overlapping (`totalTokens = input + output + cacheRead +
   cacheWrite`, verified `2+7+9133+21 = 9163`), so `input` is cache-exclusive —
   no subtraction. Independently, pi's per-component cost matched the canonical
   Anthropic per-token prices exactly (e.g. `9133 × 5e-7 = 0.0045665` cache-read),
   a second-source validation of the pricing table. Model is a bare name with a
   separate `provider` field (key `provider/model`); `auth.json[provider].type`
   gives the mode (`"api_key"` → `API_KEY`). `lifecycle.ts` must surface
   `usage.cost.total` and `provider` (it currently drops both).

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

1. **Layer 1** — schema + reader generalization: data-type additions (tokens,
   provenance, mode-split token aggregates), `compute_cost` + pricing table
   (Layer 2 table), the reader **hookspec + dispatch**, and the
   `aggregate_process_cumulative` / `aggregate_session_cumulative` utils (the
   former lifted from today's reader, unchanged). Then `mngr_claude_usage` grows
   a thin hookimpl calling `aggregate_process_cumulative` — proving the hook
   end-to-end with zero behavior change for Claude (existing tests must stay
   green).
2. **OpenCode writer** — done; `mngr_opencode_usage`, reported per-message cost
   (session-incremental). Verified live.
3. **pi writer** — done; writer in mngr_pi_coding's lifecycle extension (gated),
   `mngr_pi_coding_usage` owns the reader + gate. Reported cost. Verified live.
4. **Codex writer** — done; `mngr_codex_usage` ships `codex_usage.sh` (rollout
   `token_count` → cost_snapshot, launched by the harness supervisor when
   present) and a session-cumulative reader. Token-estimated cost + 5h/7d
   windows. OpenAI prices pinned to litellm directly. Verified live.

   **Field note (retroactive pricing):** because the writer stores tokens and the
   reader prices them at read time, a model newer than the pricing table (seen
   live: `gpt-5.5`, not yet in litellm) is captured with cost left `None` + a
   WARNING (never `$0`); once its price is added, historical events price
   retroactively. So a brand-new model degrades gracefully rather than blocking.
5. **modal_litellm cross-drift test** — done (landed alongside the pricing table).
   OpenAI prices have a separate `litellm_pricing_test` pinning them to litellm.
6. **Antigravity spike** — separate investigation; build-vs-defer decision.

## Base branch note

This work is developed on `mngr/agents-usage`, whose base is
`mngr/unify-ts-plugins` **plus** `mngr/agy-statusline` (merged in locally). The
PR should target a synthetic base branch that is exactly those two merged
together, so the antigravity statusline groundwork is treated as part of the
base rather than as a change introduced by this work.
