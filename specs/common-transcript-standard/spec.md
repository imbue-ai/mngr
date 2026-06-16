# Common transcript: alignment with the OpenTelemetry GenAI standard

**Audience:** developers working on the `mngr` common-transcript schema and the per-agent
emitters (claude, antigravity, opencode, pi-coding, codex).

**Status:** design proposal. No code has been written against it yet.

This spec proposes evolving the agent-agnostic *common transcript* schema toward the
vocabulary and message shape of the **OpenTelemetry (OTel) GenAI semantic conventions**,
rather than continuing to define every field ourselves. It is scoped to the on-disk
common-transcript records and the five emitters that produce them; it does **not** propose
adopting OTel as a transport or storage format.

Related:

- Schema: `libs/mngr/imbue/mngr/agents/common_transcript_records.py`
- Reader: `libs/mngr/imbue/mngr/cli/transcript.py`
- Mixins: `HasTranscriptMixin` / `HasCommonTranscriptMixin` in `libs/mngr/imbue/mngr/interfaces/agent.py`
- Cross-plugin feature state: [`../agent-plugin-parity/spec.md`](../agent-plugin-parity/spec.md)
- Capability detection: [`../agent-plugin-parity/capability-mixins.md`](../agent-plugin-parity/capability-mixins.md)

## Contents

- [Background](#background)
- [Why a standard, and which one](#why-a-standard-and-which-one)
- [Goals and non-goals](#goals-and-non-goals)
- [Tier 1: vocabulary alignment](#tier-1-vocabulary-alignment)
- [Tier 2: ordered `parts[]` for capable agents](#tier-2-ordered-parts-for-capable-agents)
- [Per-agent feasibility](#per-agent-feasibility)
- [Compatibility and migration](#compatibility-and-migration)
- [Impact](#impact)
- [Follow-ups](#follow-ups)
- [References](#references)

## Background

The common transcript is a JSONL stream that every agent plugin emits at
`$MNGR_AGENT_STATE_DIR/events/<agent_type>/common_transcript/events.jsonl`, so that
`mngr transcript` can render any agent's session in one agent-agnostic shape. Each line is one
record, discriminated on `type`:

- `user_message` — `content`
- `assistant_message` — `text` plus a separate `tool_calls[]` (each `tool_call_id`, `tool_name`,
  `input_preview`), with optional `model` / `usage` / `stop_reason`
- `tool_result` — `tool_call_id`, `tool_name`, `output`, `is_error`

All three share an envelope: `timestamp`, `event_id`, `source`. Records are frozen pydantic
models with `extra="allow"`, so an emitter may annotate records with its own fields (e.g.
antigravity's `conversation_id`, opencode's `message_id`). The contract is enforced at *emit*
time: each plugin ships a `test_emitted_common_records_conform_to_canonical_schema` conformance
test that drives its real emitter and asserts every record passes
`validate_common_transcript_record`. A meta-test
(`common_transcript_conformance_meta_test.py`) fails if any emitter plugin lacks that test.

The field names and message shape were defined ad hoc. This spec asks whether we can instead
track an external standard, to reduce bespoke vocabulary and make a future export to GenAI
telemetry mechanical.

## Why a standard, and which one

There is **no published RFC or on-disk standard for an agent transcript session file.** The
candidates and why they do or do not fit:

| Candidate | What it is | Verdict |
|---|---|---|
| **OpenTelemetry GenAI semantic conventions** | CNCF-backed; the only cross-vendor standard with real traction. Its v1.37 *structured message model* represents a message as `role` + an ordered `parts[]` (`text`, `tool_call`, `tool_call_response`, `reasoning`), with `finish_reason` on outputs. | Closest fit **as a vocabulary**. It is a *telemetry* schema (spans + log events emitted to a collector), **not** a session-file format, so we do not adopt it as storage — we align our field names and message shape to it. |
| OpenInference (Arize) | OTel-based LLM-observability conventions; messages flattened into indexed span attributes. | Span-only; even less suited to a standalone transcript file. |
| OpenAI / Anthropic message formats | De-facto provider wire formats. | Per-provider and divergent — the very thing the common transcript normalizes away from. |

**Conclusion:** keep our JSONL container (no standard exists for it), but align the *vocabulary*
and *message model* to OTel GenAI. This is cheap where it is a rename, removes bespoke naming,
and makes a future GenAI-telemetry exporter a mapping table rather than a re-model.

## Goals and non-goals

**Goals**

- Rename fields to match OTel GenAI vocabulary where the semantics are identical (Tier 1).
- Optionally carry an ordered `parts[]` that preserves text/tool-call interleaving, for the
  agents whose native format makes that faithful (Tier 2).
- Keep one schema that all five emitters conform to, and keep the reader tolerant.

**Non-goals**

- Adopting OTel as a transport, or emitting OTLP spans. (A future exporter is out of scope.)
- A `reasoning` part type / capturing model thinking. This is net-new extraction work per
  agent and is deferred (see [Follow-ups](#follow-ups)).
- Folding `tool_result` into a `tool`-role message with a `tool_call_response` part. We keep
  `tool_result` as a top-level record and document the 1:1 OTel correspondence instead; the
  standard-fidelity gain is cosmetic for our display use case.

## Tier 1: vocabulary alignment

Rename to match OTel without changing the message shape. Every agent is affected; the change is
mechanical.

| Today | OTel-aligned | Notes |
|---|---|---|
| `assistant_message.stop_reason` | `finish_reason` | Exact OTel term, identical semantics. Already optional (`str \| None`). |
| `role` values (`user`/`assistant`/`tool`) | unchanged | Already match OTel. |

The `tool_calls[]` field names (`tool_call_id`, `tool_name`, `input_preview`) are **deliberately
left unchanged.** Renaming `tool_call_id`/`tool_name` to OTel's `id`/`name` is cosmetic and would
force the reader to carry a fallback for old records, for no semantic gain. `input_preview` is a
*truncated preview*, not OTel's full `arguments` payload, so renaming it to `arguments` would
misrepresent the data. These names are kept; the OTel correspondence is documented here instead.

**Touch points (all five emitters):** each emitter sets `stop_reason`, so each is edited once:

- claude: `libs/mngr_claude/imbue/mngr_claude/resources/common_transcript_convert.py`
- codex: `libs/mngr_codex/imbue/mngr_codex/resources/common_transcript_convert.py`
- antigravity: `libs/mngr_antigravity/imbue/mngr_antigravity/resources/common_transcript_convert.py`
- opencode: `libs/mngr_opencode/imbue/mngr_opencode/resources/mngr_opencode_plugin.ts`
- pi-coding: `libs/mngr_pi_coding/imbue/mngr_pi_coding/resources/mngr_pi_lifecycle.ts`

Plus the schema (`common_transcript_records.py`), the five conformance tests' golden records, and
`common_transcript_records_test.py`. The reader does not display `stop_reason`, so Tier 1 does not
affect rendering.

**Note:** the TypeScript emitters (opencode, pi-coding) are under active rework. The Tier 1
renames there should be coordinated with that work; the logic is unchanged by the rename.

**Risk:** low. No information-model change, so no agent can lose fidelity.

## Tier 2: ordered `parts[]` for capable agents

OTel's structured model represents an assistant turn as an ordered `parts[]` array, so that
`text → tool_call → text → tool_call` interleaving is preserved. Our current schema splits an
assistant turn into a joined `text` string plus a separate `tool_calls[]` list, which loses that
ordering.

**Design: `parts[]` is an additive, optional field — not a replacement.**

`assistant_message` keeps `text` and `tool_calls[]` as the always-present baseline (every emitter
fills them, exactly as today). It *additionally* carries an optional ordered `parts[]` when the
emitter can produce faithful ordering:

```python
class TextPart(_RecordModel):
    type: Literal["text"]
    content: str

class ToolCallPart(_RecordModel):
    type: Literal["tool_call"]
    # field names match the flat tool_calls[] entries, so the record has one naming scheme
    tool_call_id: str
    tool_name: str
    input_preview: str

AssistantPart = Annotated[TextPart | ToolCallPart, Field(discriminator="type")]

# on AssistantMessageRecord:
parts: tuple[AssistantPart, ...] | None = None
```

The reader prefers `parts[]` when present (rendering parts in order), and falls back to the flat
`text` + `tool_calls[]` otherwise. Role derivation is unaffected (it already reads the explicit
`role` field first).

**Why additive rather than replace:**

- One schema that **every** agent conforms to; the parity gap becomes "some agents omit an
  optional field," exactly how `usage` / `model` already vary.
- The gap is **visible in the data itself** — claude and pi carry `parts[]`; the others do not —
  rather than papered over with a synthesized, fake ordering.
- It fits the schema's existing "strict core, permissive optionals" philosophy.
- It is non-breaking: adding codex/opencode later (if desired) just starts populating the field.

The alternative (replace `text`/`tool_calls[]` with a mandatory `parts[]`, lossy agents emitting a
degenerate `[text, tool_calls…]` order) is rejected: it erases the distinction between faithful
and synthesized ordering and is a breaking change for every emitter and the reader.

## Per-agent feasibility

Whether an emitter can produce a *faithful* ordered `parts[]` depends entirely on whether its
native format preserves intra-turn ordering.

| Agent | Native assistant shape | Ordered `parts[]`? |
|---|---|---|
| **claude** | `message.content[]` — ordered blocks (`text`, `tool_use`, `thinking`, …). Converter already iterates this array, then splits. | **Yes** — iterate the blocks in order instead of splitting. Easy. |
| **pi-coding** | `content: ContentBlock[]` — ordered (`text`, `toolCall`, …). Converter filters by type. | **Yes** — preserve the array order. Easy. (Coordinate with the in-flight TS rework.) |
| **opencode** | In-memory `partsByMessage` is ordered, but the emitter filters/joins by type and does not serialize order. | **Feasible, deferred** — order is available; iterate parts explicitly. Defer until the in-flight TS rework settles. |
| **codex** | Assistant messages are **text-only**; `tool_calls` is always `[]` because tool use is modeled as separate stream events (`function_call` / `function_call_output`), surfaced as standalone `tool_result` records. | **Not applicable** — there is no intra-message text/tool interleaving to preserve. A `parts[]` here would be a single text part. Cross-event ordering is a different concern, out of scope. |
| **antigravity** | `PLANNER_RESPONSE` has a scalar `content` (text) and a pre-split `tool_calls[]`; no ordering metadata, no per-call timestamps, no offsets. | **Not reconstructable** — the native format does not store where each tool call sat relative to the text. Documented as a hard limitation. |

**Decision:** implement Tier 2 for **claude and pi-coding** only. Mark codex (N/A), opencode
(feasible, deferred), and antigravity (not reconstructable) in the parity matrix.

This is reflected by adding a row to the parity matrix in
[`../agent-plugin-parity/spec.md`](../agent-plugin-parity/spec.md):

```
| Ordered assistant parts[] | Y | N (not reconstructable) | Y | deferred | N/A (text-only messages) |
```

**Aside (pre-existing, not addressed here):** codex `assistant_message` records never list their
tool calls (`tool_calls` is always `[]`); only the resulting `tool_result` records appear. This is
a property of how codex models tool use, not introduced by this change, but it is worth knowing
when reading a codex transcript.

## Compatibility and migration

- **Tier 1** is a coordinated rename across the schema, five emitters, the reader, and the
  conformance golden records, landed together. Old on-disk transcripts from already-running agents
  keep their old field names; the reader is tolerant and still renders them (it reads `stop_reason`
  nowhere for display, and would read the new tool-call field names with a fallback to the old).
- **Tier 2** is purely additive (`parts[]` optional, default `None`), so it is non-breaking by
  construction. No `schema_version` field is required: the reader's existing tolerance plus the
  prefer-`parts[]`-else-fallback rule covers both shapes.

These are live-agent logs, not long-lived archives, so a versioned-reader scheme is unnecessary;
the additive design plus reader tolerance is sufficient.

## Impact

- **Schema:** `common_transcript_records.py` — rename `stop_reason`→`finish_reason` (Tier 1); add
  the `parts[]` part union (Tier 2).
- **Emitters:** all five for Tier 1 (`finish_reason`); claude + pi-coding additionally for Tier 2.
- **Reader:** `cli/transcript.py` — prefer `parts[]` when present, else fall back to flat
  `text` + `tool_calls[]` (Tier 2). Tier 1 does not touch the reader.
- **Tests:** five conformance tests + their golden records, `common_transcript_records_test.py`.
- **Docs:** this spec; parity-matrix row in `agent-plugin-parity/spec.md`.
- **Changelog:** entries for each touched project (`mngr`, `mngr_claude`, `mngr_codex`,
  `mngr_antigravity`, `mngr_opencode`, `mngr_pi_coding`, `dev`) when implemented.

## Follow-ups

- **`reasoning` part type.** OTel defines a `reasoning` part. No emitter captures thinking today,
  but the source is sometimes available — notably antigravity's decoder already reads a
  `PLANNER_THINKING` field that the converter discards, and claude's `content[]` carries
  `thinking` blocks. Surfacing reasoning is net-new per-agent extraction and is deferred to its own
  effort.
- **codex / opencode `parts[]`.** Non-breaking to add later: codex would require modeling
  cross-event ordering (a different shape than intra-message parts); opencode is already feasible
  and just deferred behind its in-flight rework.
- **GenAI telemetry exporter.** With Tier 1 vocabulary in place, a mapping from common-transcript
  records to OTel GenAI spans/log-events becomes mechanical, if we ever want telemetry export.

## References

- OpenTelemetry GenAI semantic conventions — generative client AI spans:
  <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/>
- OpenTelemetry GenAI attribute registry:
  <https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/>
- Structured-message model discussion (role + ordered parts):
  <https://github.com/open-telemetry/semantic-conventions/issues/1913>
- OpenInference semantic conventions:
  <https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md>
