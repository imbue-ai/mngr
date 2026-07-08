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
- Carry an ordered `parts[]` on every assistant record that preserves text/tool-call interleaving,
  with a `parts_ordered` flag marking the one agent whose order is best-effort (Tier 2).
- Keep one uniform schema that all five emitters fill identically, so the reader needs no
  per-agent fallback.

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

**Risk:** low. No information-model change, so no agent can lose fidelity.

## Tier 2: universal ordered `parts[]`

OTel's structured model represents an assistant turn as an ordered `parts[]` array (text/tool_call
segments), so that `text → tool_call → text → tool_call` interleaving is preserved. The flat `text`
string plus separate `tool_calls[]` list loses that ordering.

**Design: `parts[]` is a universal field every emitter fills; `parts_ordered` marks faithfulness.**

Every emitter emits `parts[]` — the canonical, agent-agnostic ordered view of the turn, and the one
the reader renders. The flat `text` / `tool_calls[]` are kept on the same record as a convenience
baseline, but `parts[]` is authoritative for ordering, so the reader has a **single code path and no
fallback**. A `parts_ordered: bool` flags whether the order is faithful (the agent's real emission
order) or best-effort (synthesized because the native format does not record it).

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
parts: tuple[AssistantPart, ...] = ()
parts_ordered: bool = True
```

**Why universal rather than optional.** An optional `parts[]` present for only some agents would
force *every* consumer to implement a flat fallback, and ordering could never be relied on across
the common format — so it would not actually be *common*. Making `parts[]` universal keeps the
format uniform (one representation, one reader path) and is the opposite of eroding commonness. The
faithful-vs-synthesized distinction that an "only-capable-agents-emit-it" design would have encoded
*structurally* (presence/absence) is instead carried as the `parts_ordered` **metadata** flag — so
no information is lost, and antigravity's best-effort order stays honest in-band rather than silently
claiming an order it cannot know.

**Why keep the flat fields.** Dropping `text` / `tool_calls[]` for a single representation is cleaner
still, but a larger breaking change for every consumer; keeping them as a derived convenience is
low-cost and non-breaking. A future cleanup could drop them (see follow-ups).

## Per-agent ordering

Every emitter fills `parts[]`; they differ only in whether the order is faithful (`parts_ordered`).

| Agent | Native assistant shape | parts order |
|---|---|---|
| **claude** | `message.content[]` — ordered blocks (`text`, `tool_use`, `thinking`, …) | **faithful** — iterate the blocks in order |
| **pi-coding** | `content: ContentBlock[]` — ordered (`text`, `toolCall`, …) | **faithful** — iterate the array in order |
| **opencode** | in-memory parts map, populated from `message.part.updated` in arrival order | **faithful** — iterate the parts list in order |
| **codex** | assistant messages are **text-only** (tool use is separate `function_call`/`function_call_output` events, surfaced as standalone `tool_result` records) | **faithful (trivial)** — a single text part; there is no intra-message interleaving to get wrong |
| **antigravity** | `PLANNER_RESPONSE` has scalar `content` (text) + pre-split `tool_calls[]`; no ordering metadata, no per-call offsets | **best-effort** — synthesize text-then-tools; `parts_ordered=False` |

So `parts_ordered` is True for four emitters and False only for antigravity.

**Aside (pre-existing, not changed here):** codex `assistant_message` records never list their tool
calls in `tool_calls`/`parts`; tool use surfaces only as `tool_result` records. This is a property
of how codex models tool use.

This is reflected by an "Ordered assistant parts[]" row in the parity matrix in
[`../agent-plugin-parity/spec.md`](../agent-plugin-parity/spec.md).

## Compatibility and migration

- Tier 1 and Tier 2 land together: the `finish_reason` rename, the universal `parts[]` /
  `parts_ordered`, all five emitters, the reader, and the conformance/golden records.
- The reader renders an assistant turn from `parts[]` only (single path, no fallback). Old on-disk
  transcript lines written before this change lack `parts[]` and would render with an empty assistant
  body (`(no content)`). This is acceptable: common transcripts are short-lived live-agent logs, not
  archives, and are continuously re-derived from the always-on raw stream.
- `finish_reason`: the reader never displayed the stop reason, so pre-existing lines carrying the old
  `stop_reason` are unaffected at read time (the field simply lands in `extra`).
- No `schema_version` field: the format is uniform going forward, so there is nothing to branch on.

## Impact

- **Schema:** `common_transcript_records.py` — rename `stop_reason`→`finish_reason`; add the
  `parts[]` part union plus the universal `parts` and `parts_ordered` fields.
- **Emitters:** all five — `finish_reason` and `parts[]`/`parts_ordered` (claude/pi/opencode/codex
  faithful; antigravity best-effort).
- **Reader:** `cli/transcript.py` — render the assistant body from `parts[]` (single path).
- **Tests:** five conformance tests + their golden records, `common_transcript_records_test.py`, and
  the reader tests/fixtures (`cli/transcript_test.py`, `cli/testing.py`).
- **Docs:** this spec; parity-matrix row + transcript-dimension note in `agent-plugin-parity/spec.md`.
- **Changelog:** entries for each touched project (`mngr`, `mngr_claude`, `mngr_codex`,
  `mngr_antigravity`, `mngr_opencode`, `mngr_pi_coding`, `dev`).

## Follow-ups

- **`reasoning` part type.** OTel defines a `reasoning` part. No emitter surfaces thinking today, but
  the source is sometimes available — antigravity's decoder already reads a `PLANNER_THINKING` field
  the converter discards, and claude's `content[]` carries `thinking` blocks. Surfacing reasoning is
  net-new per-agent extraction and is deferred to its own effort.
- **Drop the flat `text`/`tool_calls[]`** in favor of `parts[]`-only (a single representation). Cleaner,
  but a breaking change for any consumer reading the flat fields; deferred.
- **GenAI telemetry exporter.** With the aligned vocabulary in place, mapping common-transcript
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
