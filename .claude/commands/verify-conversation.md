---
description: Review the conversation transcript for behavioral issues (misleading behavior, disobeyed instructions, instructions worth saving).
model: opus[1m]
allowed-tools: Bash:*, Read, Write, Agent
---

# Verify Conversation

Analyze the conversation transcript for behavioral issues and write the results to a hash-labeled file so the stop hook knows this commit has been checked.

## Instructions

### Step 1: Get Conversation Transcript

Run `./scripts/print_user_session.sh` to get the conversation transcript. Capture the full output.

If the output is empty (no transcript available), skip directly to Step 4 and write an empty issues file.

### Step 2: Gather Instruction Files

Read the instruction files that apply to this repository:
- `CLAUDE.md` at the repo root
- Any other instruction files (`AGENTS.md`, `.claude.md`, etc.) that exist at the repo root

These are needed so the subagent can check whether the agent obeyed them.

### Step 3: Analyze

Read the issue categories and output format from `.claude/skills/verify-conversation/categories.md`.

Spawn an Agent subagent (`subagent_type: "general-purpose"`) with a prompt that includes:

1. The full conversation transcript (from Step 1)
2. The contents of the instruction files (from Step 2)
3. The issue categories and output format (from the categories file)

Tell the subagent to:
- Review the conversation for each issue category
- Output one JSON object per line (JSONL format) for each issue found
- If no issues are found, output nothing

### Step 4: Save Results

Get the current HEAD hash: `git rev-parse HEAD`

Create the output directory: `mkdir -p .reviews/conversation`

Save the subagent's JSONL output to `.reviews/conversation/{hash}.json`. If the subagent found no issues or the transcript was empty, write an empty file (this still serves as the verification marker).

### Step 5: Report

If the output file contains any issues, summarize them briefly. For each CRITICAL or MAJOR issue with confidence >= 0.7, describe it clearly so the agent can address it.

If there are no issues, report that the conversation was verified clean.
