---
name: verify-conversation
argument-hint: [options...]
description: Review the conversation transcript for behavioral issues (misleading behavior, disobeyed instructions, instructions worth saving).
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/export_transcript_paths.sh*), Bash(python3 *${CLAUDE_PLUGIN_ROOT}/scripts/filter_transcript.py *), Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/export_transcript_paths.sh | python3 *${CLAUDE_PLUGIN_ROOT}/scripts/filter_transcript.py --total-size*), Bash(git rev-parse HEAD), Bash(wc *), Read, Write, Agent, AskUserQuestion
---

# Verify Conversation

Orchestrate a review of the conversation transcript for behavioral issues. You handle setup and coordination; an agent does the actual review.

## Arguments

If the user provides arguments, they serve as additional instructions for this run. For example:
- `/verify-conversation only review tracked sessions` -- override config to only include tracked sessions
- `/verify-conversation skip subagents` -- disable subagent transcript inclusion
- `/verify-conversation only review the current session` -- only the current session

To apply overrides, set env vars before calling the discovery script. The env vars are: `INCLUDE_TRACKED`, `INCLUDE_CURRENT`, `INCLUDE_AGENT_DIR`, `INCLUDE_SUBAGENTS` (each `true` or `false`). For example, "only tracked sessions" means:

```bash
INCLUDE_TRACKED=true INCLUDE_CURRENT=false INCLUDE_AGENT_DIR=false INCLUDE_SUBAGENTS=false bash ${CLAUDE_PLUGIN_ROOT}/scripts/export_transcript_paths.sh
```

## Instructions

### Step 1: Find Session Files

Run the export transcript script to discover session file paths:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/export_transcript_paths.sh
```

The script outputs lines in the format `source\tpath`, where source is one of: `mngr_tracked`, `current`, `mngr_agent_dir`, or a subagent variant like `mngr_tracked:subagent`, `current:subagent`, etc. Parse each line to collect the files grouped by source.

If this outputs nothing (no sessions found), skip to Step 5 and write an empty marker file.

### Step 2: Check Size and Choose Model

Get the total filtered size across all session files by piping the export script output to the filter script:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/export_transcript_paths.sh | python3 ${CLAUDE_PLUGIN_ROOT}/scripts/filter_transcript.py --total-size
```

This outputs a single number (total bytes).

- If total size exceeds 3MB (3000000 bytes), STOP and warn the user. The transcripts are too large for even the 1M context window. Suggest narrowing scope, for example:
  - `/verify-conversation only review tracked sessions`
  - `/verify-conversation skip subagents`
  - Disabling some sources in `.reviewer/settings.json` under `verify_conversation` (or `.reviewer/settings.local.json` for local-only overrides)

  Do NOT proceed unless the user confirms they want to try anyway.

- If total size is 200KB or more (but under 3MB), use `model: "opus[1m]"` -- the transcript needs a larger context window.
- If total size is under 200KB, use `model: "opus"` -- the transcript comfortably fits in a standard context window.

### Step 3: Gather Progress Data

Use the Read tool to read `.reviewer/outputs/conversation/progress.jsonl`. This file tracks which parts of the transcript have already been reviewed. If it exists, for each session file from Step 1, compare the current line count (`wc -l <path>`, not `wc -l < <path>`) against the line count recorded in the progress file.

### Step 4: Spawn Agent

Get the current HEAD hash: `git rev-parse HEAD`

Spawn a `review-conversation` Agent and tell it to:

1. Read the instruction files: `CLAUDE.md` at the repo root, plus any other instruction files (`AGENTS.md`, `.claude.md`, etc.) that exist at the repo root

Also provide the agent with:

2. The issue categories path: `.reviewer/conversation-issue-categories.md` if it exists, otherwise `${CLAUDE_PLUGIN_ROOT}/agents/categories/conversation-issue-categories.md`
3. The filter script path: `${CLAUDE_PLUGIN_ROOT}/scripts/filter_transcript.py`
4. The list of session file paths to read, grouped by provenance:
   - `mngr_tracked` files: label as "The sequence of tracked session files for this task"
   - `current` files: label as "The current session"
   - `mngr_agent_dir` files: label as "All sessions found in this agent's directory"
   - Any source ending in `:subagent`: label as "Subagent transcripts" (grouped under their parent source)
5. The output file path: `.reviewer/outputs/conversation/{hash}.json`

If the progress file exists:
- Include the progress data in the prompt
- Tell the agent: "The following portions have already been reviewed. You should only review the parts that have NOT been reviewed yet, but you may look at already-reviewed portions for context if needed."
- For files that have grown since last reviewed, tell the agent the previously-reviewed line range (e.g. "lines 1-500 already reviewed") so it can focus on the new content.
- For files whose line count has NOT changed, tell the agent it can skip that file entirely (but may reference it for context).

If there is no progress file, tell the agent to review all session files in full.

### Step 5: Update Progress

After the agent finishes, update the progress file (`.reviewer/outputs/conversation/progress.jsonl`).

For each session file that was part of this review, get its current line count (`wc -l`). Then use the Write tool, without checking if the directory exists, to update `.reviewer/outputs/conversation/progress.jsonl`, appending a JSONL line per file:

```json
{"file": "<session_file_path>", "lines": <total_line_count>, "reviewed_at": "<ISO 8601 timestamp>"}
```

This ensures the next invocation knows which portions have already been covered. If a file already has an entry in the progress file, the newer entry takes precedence (the top-level agent should always use the latest entry per file).

### Step 6: Save Results

If the agent found no issues or no transcript was available, use the Write tool (without checking if the directory exists) to ensure the output file `.reviewer/outputs/conversation/{hash}.json` exists (even if empty) -- it serves as the verification marker.

### Step 7: Report

If the output file contains any issues, summarize them briefly. For each CRITICAL or MAJOR issue with confidence >= 0.7, describe it clearly so the agent can address it.

If there are no issues, report that the conversation was verified clean.
