# Robust subagent linkage in minds_workspace_server

> Make subagent rendering in minds_workspace_server work for all Claude Code subagent types and versions, by reading the `tool_use_id → subagent_id` link from both the structured `toolUseResult.agentId` field and the `agentId:` text trailer (preferring the structured field), instead of relying on the trailer regex alone.

## Overview

- Today, minds links a parent Agent tool_use to its subagent session by regex-scraping the `agentId: <id>` text trailer Claude Code optionally appends to Agent tool_result content. When the trailer is absent, the frontend falls back to the boring inline render and loses the nice subagent card.
- Claude Code omits the trailer for one-shot subagent types (e.g. `Explore`) in some versions, so even fully successful Agent calls render badly. The bug is visible in main agents that use `Explore` subagents while not affecting worker agents that use `general-purpose`.
- Empirical scan of all on-disk Claude session files shows neither source is complete: the trailer covers ~93% of Agent tool_results, the structured field `toolUseResult.agentId` covers ~64%, and together they cover 100%. The structured field is universally absent from nested-subagent jsonls; the trailer is absent from older versions and from some recent non-resumable agent types.
- Fix: read both. Prefer the structured field (no string parsing, no fragility against trailer text changes), fall back to the trailer regex. The rest of the pipeline (`_enrich_subagent_metadata`, frontend card rendering) is unchanged.
- This is purely a Claude Code adapter change — `session_parser.py` and `session_watcher.py` already encode Claude-Code-specific schema throughout, so there is no abstraction being broken.

## Expected behavior

- Subagents of every Claude Code agent type (`Explore`, `general-purpose`, plugin-defined types like `imbue-code-guardian:verify-and-fix`, etc.) render as the rich subagent card in the minds frontend, not as a plain inline tool-call block.
- The card shows the same fields as today (agent type, description); no visual changes.
- Sessions captured by older Claude Code versions that emitted only the trailer continue to render correctly.
- Sessions captured by newer Claude Code versions that emit only the structured field also render correctly — this is the case that fails today.
- Previously-recorded sessions on disk get the improved rendering automatically the next time the user opens the mind, because minds re-parses session files on each `get_all_events` call.
- When the structured field and the trailer disagree (not expected to occur, but defensively): the structured field wins silently.
- No change to behavior for any tool other than `Agent`.

## Changes

- Update the Agent tool_result parsing path in `session_parser.py` so that `subagent_id` is extracted from `toolUseResult.agentId` on the raw event when present, with the existing `agentId:` text-trailer regex retained as a fallback when the structured field is missing.
- Preserve current event shape: `subagent_id` is still attached to the same `tool_result` event field that `_enrich_subagent_metadata` already consumes — no downstream changes needed.
- No changes to `session_watcher.py`, no changes to the frontend, no changes to `.meta.json` reading.
- Add a unit test alongside the existing parser tests covering three fixture cases:
  - Agent tool_result with `toolUseResult.agentId` only (no text trailer) → `subagent_id` extracted.
  - Agent tool_result with text trailer only (no `toolUseResult.agentId`) → `subagent_id` extracted via fallback.
  - Agent tool_result with neither → no `subagent_id` field on the emitted event.
- Manual verification by spawning an `Explore` subagent in a real minds session and confirming the rich subagent card renders.
