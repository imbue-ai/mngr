# Group 4: agents & work

---

## 1. Workers / Delegation / Tasks — launch-task, worker branches, per-dispatch runtime dirs, merge gates

### 1.1 Canonical Definition

A **worker** is a short-lived mngr agent created by the `launch-task` skill (or its derivatives `crystallize-task`, `heal-skill`, `update-skill`) to execute a single bounded task in its own git worktree and branch. The worker is created via `mngr create <name> -t <template>` with a git-worktree transfer, lives under the branch `mngr/<name>`, and communicates results back to the **lead agent** exclusively through a report file (`finish_report_path` declared in the task file's frontmatter).

Sources:
- `.agents/skills/launch-task/scripts/create_worker.py`:6-77 — canonical module docstring defining "worker" and the two-phase lifecycle (`launch` + `await`)
- `.agents/skills/launch-task/SKILL.md`:1-155 — skill recipe defining the dispatch pattern
- `.agents/skills/launch-task/scripts/create_worker.py`:316-346 — the exact `mngr create / mngr rsync / mngr message` command sequence that constitutes a "launch"

### 1.2 All Usages

**The "worker" concept has three distinct layers:**

**Layer A — The mngr agent itself (the live process):**
- Created by `mngr create <name> -t <template> --label workspace=<workspace>` (create_worker.py:316-327)
- Branch: `mngr/<name>` (SKILL.md launch-task:10)
- Template used determines agent type: `worker`, `crystallize-worker`, etc. (create_worker.py:427)
- Lives until explicitly destroyed by the lead after merge; a STOPPED worker with uncommitted work can be resumed via `mngr start` (SKILL.md launch-task:147-150)

**Layer B — The per-dispatch runtime directory:**
- All task artifacts live under `runtime/launch-task/<name>/` (SKILL.md launch-task:10)
- Task file: `runtime/launch-task/<name>/task.md`
- Reports land at the path declared in `finish_report_path` frontmatter — typically `runtime/launch-task/<name>/reports/report.md`
- The lead syncs this directory into the worker via `mngr rsync ./runtime/launch-task/<name>/ <name>:runtime/launch-task/<name>/` (create_worker.py:234-266)

**Layer C — The merge gate / branch lifecycle:**
- Worker does work on `mngr/<name>` branch
- On terminal `done` report, lead merges the worker's branch into its own branch (SKILL.md crystallize-task:264, SKILL.md launch-task:Steps 4 guidance)
- Merged branch is then deleted (`mngr destroy` / standard mngr lifecycle)

**Derivative worker flows:**
- `crystallize-task`: worker named `crystallize-<slug>`, template `crystallize-worker`, runtime at `runtime/crystallize/<slug>/`
- `heal-skill`: worker named `heal-<target>`, template `crystallize-worker`, runtime at `runtime/heal/<target>/`
- `update-skill`: worker named `update-<target>`, template `crystallize-worker`, runtime at `runtime/update/<target>/`

### 1.3 Competing / Multiple Definitions

**AMBIGUITY: "task" is severely overloaded.** The word "task" appears in four distinct senses in this codebase:

1. **Task as a skill** — `launch-task`, `crystallize-task`, `heal-task` are *skill names* (the skill's slug is "task")
2. **Task as the work unit** — "a task" is a bounded piece of work assigned to a worker (the `task.md` file)
3. **Task as a ticket type** — `tk create -t task` creates a ticket of type "task" (the default type in `vendor/tk/ticket`:239)
4. **Sub-agent / background agent** — the FCT CLAUDE.md (line 6) refers to agents launched via `launch-task` as "sub-agents" but the memory file `feedback_minds_terminology_background_agents.md` notes the correct term is "background agents" (not sub-agents, which are a Claude Code harness feature)

**The `launch-task` SKILL.md itself** uses both "sub-agent" and "worker" to refer to the launched agent (lines 1-3 description says "sub-agent"; step 0 says "sub-agent"; step 2 says "worker"). This is an unresolved inconsistency within a single file.

### 1.4 Terminology Variants

| Term used | Location | Meaning |
|---|---|---|
| `worker` | create_worker.py:6, launch-task/SKILL.md:9 | The mngr agent doing the delegated work |
| `sub-agent` | launch-task/SKILL.md:3,15,18,22 | Same as worker (inconsistent synonym) |
| `background agent` | memory file `feedback_minds_terminology_background_agents.md` | Preferred canonical term per memory |
| `lead agent` / `lead` | create_worker.py:23, SKILL.md crystallize-task:96 | The delegating agent (contrast: worker) |
| `task` | launch-task/SKILL.md:10 | Both the dispatch name AND the unit of work |
| `task file` | create_worker.py:32, SKILL.md:75 | The `task.md` file handed to the worker |
| `template` | create_worker.py:425-427 | The `mngr create -t <template>` type; e.g. `worker`, `crystallize-worker` |

### 1.5 Ambiguities / Inconsistencies

1. **"sub-agent" vs "worker" vs "background agent"**: Three terms for the same concept in active use. `launch-task/SKILL.md` uses "sub-agent" in its description but "worker" in its body. The memory policy prefers "background agent". No single authoritative source enforces the term.

2. **Task = skill AND task = work unit**: The skill `launch-task` delegates a "task" (work unit), and the resulting delegation's name slug is itself "task". Every derivative (`crystallize-task`, `heal-task`) amplifies this confusion: "heal-task" is a skill that launches a worker that heals a skill.

3. **The report-file contract vs the branch**: The worker-lead protocol uses the report file as the completion signal, but the branch merge is the delivery mechanism. There is no enforcement that the report arrives before the merge — the lead decides independently.

### 1.6 DOC/CODE DIVERGENCES

- **DOC/CODE DIVERGENCE**: `launch-task/SKILL.md` line 3 description says "Create a sub-agent"; step 0 (line 18) says "Delegate ... to a sub-agent"; but the module docstring of `create_worker.py:6` uses "Worker-creation driver" and every internal variable/function name uses "worker". The description field is user-facing and displayed by the skills system to trigger the skill, while the implementation calls it a worker. The two documents use different vocabulary for the same entity.

### 1.7 Recommended Canonical Term

**Canonical term: "worker"** for the launched agent entity; **"task dispatch"** for the action of creating a worker and handing it a task. The "sub-agent" usage in SKILL.md descriptions should be updated to "background agent" (per memory policy) or "worker". The word "task" should be avoided as a standalone noun for the dispatch concept — "worker dispatch" or "task delegation" is clearer. The `task.md` file is a "task brief" or "task file" (both are used).

---

## 2. Tickets — tk records, cross-agent work units, assignment, schema

### 2.1 Canonical Definition

A **ticket** is a markdown file with YAML frontmatter stored under the directory pointed to by `$TICKETS_DIR` (default: `<workdir>/.tickets/`, typically overridden to `runtime/tickets/` in the Minds context). Tickets are managed by the `tk` CLI (`vendor/tk/ticket`, a single-file bash script ~1000 lines). The data model is defined by two sources that must be read together: the bash `cmd_create()` function (vendor/tk/ticket:235-371) and the Python `TicketState` pydantic model that parses files the bash writes (FCT:apps/system_interface/imbue/system_interface/tickets_parser.py:50-88).

### 2.2 Ticket Schema

Defined in `TicketState` (tickets_parser.py:50-88):

```python
class TicketState(FrozenModel):
    ticket_id: str      # frontmatter `id:` field, e.g. "tt-2efd" or "cod-step-f1zl"
    title: str          # first H1 line in the body
    status: str         # "open" | "in_progress" | "closed"
    created_at: str     # frontmatter `created:`, ISO-8601
    started_at: str     # frontmatter `started:`, set by `tk start` (empty if not started)
    closed_at: str      # frontmatter `closed:`, set by `tk close` (empty if not closed)
    summary: str | None # text of the `## Summary` section (written by `tk close <id> "..."`)
    agent: str          # frontmatter `agent:` = $MNGR_AGENT_NAME of creator (empty if outside mngr context)
    step: bool          # frontmatter `step: true` marks turn-bound progress records
    parent_id: str      # frontmatter `parent:` = parent ticket id, or empty
    assignee: str       # frontmatter `assignee:` = who is currently working it
```

Additional fields written by `cmd_create()` but NOT in `TicketState` (vendor/tk/ticket:324-368):
- `deps: []` — dependency list
- `links: []` — symmetric link list
- `type:` — ticket type (bug|feature|task|epic|chore)
- `priority:` — 0-4 integer
- `external-ref:` — e.g. "gh-123"
- `tags: [...]` — comma-separated tags

These fields are invisible to the Python watcher infrastructure (only relevant to `tk ls`, `tk blocked`, `tk dep`, etc.).

### 2.3 ID Format

IDs are generated by `generate_id()` (vendor/tk/ticket:93-123):
- Regular ticket: `<prefix>-<4-char-alnum>` e.g. `cod-f1zl`
- Step record: `<prefix>-step-<4-char-alnum>` e.g. `cod-step-f1zl`

The `-step-` segment is load-bearing: it lets a consumer recognize a step record by its id alone, without reading the file's `step: true` frontmatter (vendor/tk/ticket:95-97).

Prefix is derived from the first letter of each hyphenated segment of the current directory name (vendor/tk/ticket:106-122).

### 2.4 All Usages

**The ticket CLI (`tk`):**
- `tk create [title]` — create regular ticket (unassigned in mngr context)
- `tk create --step "..."` — create step record (turn-bound, auto-nests under in_progress ticket)
- `tk start <id>` — set in_progress + auto-self-assign (for tickets) + stamp `started:` timestamp
- `tk close <id> [summary]` — set closed + stamp `closed:` + write `## Summary` section (required for steps)
- `tk ls / tk ready / tk blocked / tk closed` — bulk listing (steps hidden by default)
- `tk steps` — list this agent's open step records
- `tk assign / tk unassign` — explicit assignment
- `tk dep / tk link / tk show` — dependency/link management

**The Python watcher infrastructure** (FCT only):
- `tickets_parser.parse_ticket_text(text)` — parse a ticket file to `TicketState`
- `tickets_parser.parse_ticket_file(path)` — read + parse a ticket file
- `AgentTicketsWatcher` (tickets_watcher.py) — monitors an agent's `.tickets/` directory; emits `step_enrichment` SSE events

**The `tk` CLI** is the authoritative write path; the Python parser is read-only infrastructure used by the system_interface server to surface step enrichment in the chat progress view.

**TICKETS_DIR resolution** (agent_discovery.py:120-147):
1. `TICKETS_DIR` in agent's per-agent env file (`<agent_state_dir>/env`)
2. `TICKETS_DIR` in the system_interface process environment (set via `host_env` in `.mngr/settings.toml`)
3. `<work_dir>/.tickets` (tk default)

In Minds deployments: `TICKETS_DIR=/code/runtime/tickets` (comment in agent_discovery.py:128-130), so tickets ride the runtime-backup branch.

### 2.5 Is there a `tk` CLI as a standalone tool?

Yes. `tk` is the command name; the underlying script is `vendor/tk/ticket` (a vendored copy of the open-source `ticket` tool from github.com/wedow/ticket, also available via Homebrew). The `tk` name is installed as a symlink or alias. There is NO separate `tk` Python package in the mngr libs — `tk` is bash-only. The Python `tickets_parser.py` and `tickets_watcher.py` in FCT's `system_interface` are the *reading* side; they do not use any `tk` Python library.

### 2.6 Terminology Variants

| Term | Where | Meaning |
|---|---|---|
| `ticket` | tickets_parser.py:1, vendor/tk/ticket:2 | Any `.md` file in `.tickets/` with valid frontmatter |
| `regular ticket` | FCT CLAUDE.md (task management section) | A ticket without `step: true` — cross-agent, routed by assignee |
| `step record` | FCT CLAUDE.md, tickets_watcher.py:1 | A ticket with `step: true` — creator-private, turn-bound |
| `work unit` | FCT CLAUDE.md (task management section) | Synonym for regular ticket in prose descriptions |
| `task` | TicketState.ticket_id prefix in parser module docstring | Also used for the ticket type `type: task` |
| `issue` | vendor/tk/README.md | Legacy term from beads migration; no longer used in code |

### 2.7 Ambiguities / Inconsistencies

1. **The `TicketState` model parses only a subset of frontmatter fields.** Fields like `deps`, `links`, `type`, `priority`, `tags` are written by `tk` but invisible to `TicketState`. The Python consumer only knows status/title/timestamps/agent/step/parent/assignee. This means any code consuming `TicketState` has a blind spot on the full schema.

2. **`tk create` in an mngr context leaves `assignee:` empty** for regular tickets (auto-assignment only on `tk start`), but **outside mngr context** it defaults `assignee` to `git config user.name` (vendor/tk/ticket:248-250). This is a behavior divergence based on environment.

3. **The `type: task` field** (default ticket type) is unrelated to the concept of "task" as in `launch-task`. A ticket of `type: task` and a `launch-task` worker task are completely different things that share a word.

### 2.8 DOC/CODE DIVERGENCES

No significant divergences found. The `tickets_parser.py` module docstring (lines 1-31) accurately describes the file format and the `tk close` summary mechanism.

### 2.9 Recommended Canonical Term

**"ticket"** for the markdown record entity. **"step record"** for `step: true` tickets (not "step ticket" or "step"). **"regular ticket"** for non-step tickets. The word **"task"** should be avoided for tickets since it conflicts with the `task.md` dispatch concept.

---

## 3. Steps — turn-bound tk --step progress records

### 3.1 Canonical Definition

A **step record** (also called "step" informally) is a special kind of ticket created with `tk create --step "..."`. It is distinguished from regular tickets by:
- `step: true` in frontmatter (tickets_parser.py:78, vendor/tk/ticket:343)
- An ID containing the literal `-step-` segment (vendor/tk/ticket:119)
- Creator-private visibility: surfaces only to the agent whose `$MNGR_AGENT_NAME` matches the `agent:` frontmatter field (tickets_watcher.py:236-241)
- Mandatory close summary: `tk close <id> "summary"` requires a non-empty summary for steps (vendor/tk/ticket:461-467)
- Auto-nesting under in-progress tickets (vendor/tk/ticket:280-308)

### 3.2 Relationship to Tickets and Turns

Steps are "turn-bound progress markers" (tickets_parser.py:76, FCT CLAUDE.md). The relationship:

```
Turn (Claude Code conversation turn)
  └── Step records (1..N, sequential)
        └── May nest under a regular ticket (via parent_id / parent: frontmatter)
```

The chat progress view in the system_interface frontend derives *structure* (which steps exist, their order, open/close transitions) from the **session transcript** (the Claude Code session JSONL files parsed by session_watcher.py). The `.tickets/` files provide **enrichment** only: canonical title, close summary, status, and creation timestamp.

Source: tickets_watcher.py:1-23 (module docstring, explicit on the structure-vs-enrichment split).

### 3.3 Step Lifecycle

```
open (created by tk create --step "...")
  → in_progress (tk start <id>)  -- stamps started: timestamp
  → closed (tk close <id> "summary")  -- stamps closed: timestamp, writes ## Summary
```

There is no "failed" status. All steps close as "closed" regardless of outcome (FCT CLAUDE.md: "No 'failed' status" section).

Steps persist across user turns until explicitly closed. The session reminder hook surfaces unclosed steps at the start of each turn.

### 3.4 Step Enrichment Snapshot

`AgentTicketsWatcher` (tickets_watcher.py:79-249) maintains a per-agent snapshot of step enrichment:

```python
{ticket_id: {"title": str, "summary": str | None, "status": str, "created_at": str}}
```

This snapshot is:
- Served on demand via `get_enrichment()` on every `GET /events` request (tickets_watcher.py:126-139)
- Broadcast as SSE `step_enrichment` messages whenever it changes (tickets_watcher.py:46, 148-150)

The `created_at` field uses fixed-width microsecond UTC ISO-8601 for lexicographic chronological ordering (tickets_watcher.py:49-52). The watcher falls back to file mtime if `created:` is absent or malformed (tickets_watcher.py:219-222).

### 3.5 Filtering Rule

A step is shown only to its creator agent:
- If `state.agent` is non-empty and `state.agent != self._agent_name`: skip (tickets_watcher.py:236-241)
- If `state.agent` is empty (pre-`agent:` field tk version): show to all agents (backward compatibility)

Regular tickets (non-step) are shown to the assignee, not the creator (this logic is NOT in the watcher — regular tickets are excluded from the watcher's step-only snapshot entirely).

### 3.6 Terminology Variants

| Term | Where | Meaning |
|---|---|---|
| `step record` | tickets_parser.py:76, tickets_watcher.py:1 | Canonical Python-layer term |
| `step` (noun) | FCT CLAUDE.md (task management section) | Informal name used in prose |
| `step` (field) | TicketState.step, frontmatter `step: true` | The boolean that marks a step record |
| `turn-bound progress record` | tickets_parser.py:76 | Descriptive phrase, not a term of art |
| `progress marker` | FCT CLAUDE.md | General informal synonym |
| `tk --step` | FCT CLAUDE.md, vendor/tk/ticket:265 | The CLI flag; sometimes used as the concept name |

### 3.7 Ambiguities / Inconsistencies

1. **"step" is used as noun (step record), adjective (step ticket), verb (progress step), and field name (`step: true`).** In FCT CLAUDE.md, "step" alone means "step record." In general conversation about skills, "step" means a numbered item in a process (e.g., "Step 1: do X"). These collide in context.

2. **Steps and `TodoWrite`**: The FCT CLAUDE.md explicitly says `tk create --step` is the replacement for Claude Code's `TodoWrite` (line ~6 in task management section). But `TodoWrite` items are not tickets at all — this mapping is conceptually clean but creates a naming tension where the UI progress view is described both in terms of "steps" (tk) and what looks like todos (the user mental model).

3. **The enrichment snapshot does not carry position/ordering** — position is transcript-derived. This means the `created_at` field in the enrichment exists only for ordering *pending (not-yet-started) steps*, not for all steps.

### 3.8 DOC/CODE DIVERGENCES

No significant divergences. tickets_watcher.py module docstring (lines 1-23) is accurately implemented.

### 3.9 Recommended Canonical Term

**"step record"** as the technical term (distinguishes from regular tickets). **"step"** as acceptable shorthand in prose. Never use "step ticket" (ambiguous with ticket-of-type-step vs. a step that is also a ticket). The field name `step: true` should stay to match the existing wire format.

---

## 4. Reviews — worker code review (.reviewer settings)

### 4.1 Canonical Definition

"Reviews" in this codebase refers to the **code review gate** run by the `imbue-code-guardian` plugin at turn-end. This is NOT a mngr feature — it is a Claude Code harness feature configured via `.reviewer/settings.json`. Reviews are run by two distinct mechanisms:

1. **Autofix** (`/autofix` skill): automatically finds and fixes code issues flagged by the reviewer
2. **Verify-architecture**: assesses whether the approach taken is correct
3. **Verify-conversation**: reviews the conversation transcript for behavioral issues

The code review categories are defined in `.reviewer/code-issue-categories.md` (both in the main repo and in FCT).

### 4.2 Review Configuration

`.reviewer/settings.json` (same content in both the main repo `/code/.reviewer/settings.json` and FCT `/.external_worktrees/forever-claude-template/.reviewer/settings.json`):

```json
{
    "stop_hook": {
        "enabled_when": "test -n \"${MNGR_AGENT_STATE_DIR:-}\" || test -n \"${SCULPTOR_API_PORT:-}\""
    },
    "autofix": {"is_enabled": true, "append_to_prompt": ""},
    "verify_conversation": {
        "is_enabled": true,
        "include_tracked_sessions": true,
        "include_current_session": true,
        "include_all_agent_sessions": true,
        "include_subagents": true
    },
    "ci": {"is_enabled": true}
}
```

The stop hook fires when `MNGR_AGENT_STATE_DIR` is set (inside an mngr agent) or `SCULPTOR_API_PORT` is set (inside a Sculptor session).

### 4.3 Review Code-Issue Categories

Defined in `.reviewer/code-issue-categories.md`, the categories include:
- `commit_message_mismatch` — diff doesn't fulfill the request
- `commit_contents` — inappropriate diff contents (binaries, unrelated changes)
- `documentation_implementation_mismatch` — docstrings/docs don't match code
- `incomplete_integration_with_existing_code` — architectural pattern violations
- `user_request_artifacts_left_in_code` — change-tracking comments in code
- `poor_naming` — naming convention violations
- `repetitive_or_duplicate_code`
- `refactoring_needed`
- `test_coverage` / `test_quality`
- `resource_leakage`
- `dependency_management`
- `insecure_code`
- `fails_silently`
- `instruction_file_disobeyed`
- `abstraction_violation`
- `logic_error`
- `runtime_error_risk`
- `incorrect_algorithm`
- `error_handling_missing`
- `async_correctness`
- `type_safety_violation`
- `correctness_syntax_issues`

### 4.4 The Worker Review Gate (crystallize-task worker, Stage 5)

When crystallization workers build a new skill, they run an internal review gate at Stage 5:
1. Run `/autofix` on their commits
2. Run `/imbue-code-guardian:verify-architecture` on the branch
3. Report findings in Gate 2 (final-artifact)

Source: crystallize-task/assets/worker/SKILL.md:165-173.

### 4.5 Relationship to Worker Branches / merge gates

The `.reviewer` CI gate (`"ci": {"is_enabled": true}`) runs on PRs in CI. For worker branches (`mngr/<name>`), the lead merges the worker's branch locally (not via PR), so the CI reviewer gate does NOT apply to worker branches. The reviewer gate applies to branches that go through GitHub PRs — i.e., the primary agent's development branches.

### 4.6 Ambiguities / Inconsistencies

1. **"review" means different things in different contexts:**
   - In everyday language: "review the output" = human reading
   - In `.reviewer/`: the automated code review gate
   - In crystallize-task worker Stage 5: running `/autofix` and `verify-architecture` as a quality gate before user approval
   - In the lead-proxy flow: the lead "reviews" the worker's report before approving a gate

2. **There is no mngr-native review concept.** The `.reviewer` system is purely a Claude Code harness add-on. The mngr library (`libs/mngr/`) has no "review" concept. This means "reviews" as a concept in this taxonomy refers to the harness plugin, not to a mngr primitive.

### 4.7 DOC/CODE DIVERGENCES

No significant divergences found. The `.reviewer/settings.json` content is identical between the main repo and FCT, and the code-issue categories file is the canonical definition.

### 4.8 Recommended Canonical Term

**"code review gate"** for the `.reviewer`-driven automated review. **"review gate"** in crystallize-task worker context. Distinguish clearly from "user approval gate" (the Gate 1/Gate 2 report-based mechanism in worker flows). These are two orthogonal review concepts that the word "review" conflates.

---

## 5. Skill Lifecycle — crystallize-task, heal-skill, update-skill, do-something-new; crystallized metadata; scenario testing

### 5.1 Canonical Definition

A **skill** is a directory under `.agents/skills/<name>/` containing a `SKILL.md` (required) and optional `scripts/run.py`, `references/`, and `assets/`. Skills follow the [agentskills.io specification](https://agentskills.io/specification).

The **skill lifecycle** defines four lifecycle operations and three states:

**States:**
- **Uncrystallized (hand-authored)**: A skill written directly by a human or agent, without going through the crystallization pipeline. No `metadata.crystallized: true` in frontmatter.
- **Crystallized**: A skill produced by `crystallize-task` (or equivalent). Carries `metadata.crystallized: true` in SKILL.md frontmatter (spec-summary.md:44-45). A `scripts/run.py` is optional even for crystallized skills (a skill may be pure prose); if present, it must begin with a PEP 723 header (validate_skill.py:16-17, 67-74).
- **Broken/stale**: A skill that fails or produces wrong results (requiring `heal-skill`) or is outdated (requiring `update-skill`).

**Lifecycle Operations:**

| Operation | Skill | Trigger | Worker template |
|---|---|---|---|
| Create (research path) | `do-something-new` | Net-new task needing research | None (lead does it directly, then crystallizes) |
| Crystallize | `crystallize-task` | Turn just finished is cohesive and repeatable | `crystallize-worker` |
| Fix (broken) | `heal-skill` | Skill errored or produced wrong result | `crystallize-worker` |
| Extend/update | `update-skill` | Skill needs new capability or verify change | `crystallize-worker` |

### 5.2 The `metadata.crystallized: true` Flag

Defined in spec-summary.md:44-45 and validated by `validate_skill.py` (FCT `.agents/shared/scripts/`).

Presence of `metadata.crystallized: true` in SKILL.md frontmatter:
1. Signals the skill was produced by the crystallize pipeline (not hand-authored)
2. Does NOT require `scripts/run.py` — the validator explicitly allows crystallized pure-prose skills (validate_skill_test.py:87-92, `test_crystallized_without_run_py_is_ok`). `run.py` is only validated for its PEP 723 header *when present* (crystallized or not).
3. Does NOT mean the skill is currently working correctly (it may need `heal-skill`)

Source: spec-summary.md:40-48, validate_skill_test.py:26-41.

### 5.3 Scenario Testing

Scenarios are *ephemeral* — they exist only in the agent's transcript, not on disk (spec-summary.md:90-103, crystallize-task-worker/SKILL.md:119-128). The format:

```
### Scenario: <description>
- Command: `uv run .agents/skills/<name>/scripts/run.py <args>`
- Input: ...
- Expected: ...
- Actual: ...
- Status: pass | fail
```

For skills that parse external data (HTML, API JSON, etc.), fixture-based unit tests ARE written to disk under `.agents/skills/<name>/tests/fixtures/` (crystallize-task-worker/SKILL.md:141-165).

### 5.4 All Usages

**`do-something-new`** (SKILL.md:1-312):
- Triggers on: net-new task needing research/experimentation
- Phases: clarification → research → plan proposal → validate dependencies → sample loop → crystallize (background) + surfaces
- The sample is saved to `runtime/do-something-new/<slug>/sample.json`
- Kicks off `crystallize-task` at Step 6 with `source_artifacts_dir: runtime/do-something-new/<slug>/`

**`crystallize-task`** (SKILL.md:1-296, worker SKILL.md:1-223):
- Stages (worker): Replicate → Outline (Gate 1) → Build artifact → Scenarios → `/autofix` + `verify-architecture` → Final artifact (Gate 2) → Commit + `done` report
- Two user approval gates: `outline-approval` (Gate 1), `final-artifact` (Gate 2)
- On `done`: lead merges `mngr/crystallize-<slug>` branch; runs post-crystallize migration (clean up runtime dir, update consumer references, close tracking ticket)

**`heal-skill`** (SKILL.md:1-169, worker SKILL.md):
- Triggers: skill errored, wrong result, prose ambiguous, missing capability
- Single user approval gate: `final-artifact` only (no outline gate)
- Worker: replicates the incident, finds root cause, fixes, runs 2-3 fresh scenarios

**`update-skill`** (SKILL.md:1-120):
- Two flows:
  - **absorb**: skill ran but extra repeatable work was needed → worker proposes design (Gate 1) + implements (Gate 2)
  - **verify**: user + agent agreed on change and it was applied live → worker skips Gate 1, runs scenarios, presents Gate 2
- Terminal status `no-update-needed` (unique to update-skill): close ticket, no merge

**`validate_skill.py`** (FCT `.agents/shared/scripts/`):
- Checks: SKILL.md present, frontmatter valid, name matches directory, kebab-case name rules, description length 1-1024 chars, body ≤ 500 lines, and — *if* `scripts/run.py` exists — that it begins with a PEP 723 header. `run.py` is NOT required even for crystallized skills (validate_skill.py:14-17, 67-74)
- Used in crystallize-task-worker Stage 3 (worker/SKILL.md:112-115)

**`detect_crystallization_candidate.py`** (FCT `scripts/`):
- Used by stop hook to detect when a turn is a crystallization candidate
- Checks for `metadata.crystallized: true` in existing skills (detect_crystallization_candidate.py:237-241)

### 5.5 Competing / Multiple Definitions

**"skill" is overloaded between the Claude Code harness skills system and the agentskills.io system:**
- The main repo's Claude Code harness has "skills" as prompts injected into the agent context (the Sculptor/sculptor-workflow skills)
- FCT's `.agents/skills/` are agentskills.io-spec skills (SKILL.md + optional scripts)
- Both are called "skills" and both run via Claude Code's `Skill` tool, but they are different conventions

### 5.6 Ambiguities / Inconsistencies

1. **Crystallized vs hand-authored distinction matters for validation** (`metadata.crystallized: true` requires `scripts/run.py`) but does NOT affect runtime behavior — the agent reads SKILL.md either way. This is a metadata convention, not a functional distinction.

2. **The crystallize pipeline is invoked at turn-end** (FCT CLAUDE.md: "live first, ratify at turn-end"), but the `crystallize-task` SKILL.md has a "Step 1: Confirm" gate that asks the user before proceeding. This means the CLAUDE.md guidance ("invoke crystallize-task at turn-end after stop-hook nudge") and the skill's own Step 1 can both fire, potentially asking the user twice. The SKILL.md Step 1 includes skip conditions for this case (crystallize-task/SKILL.md:67-80).

3. **`do-something-new` delegates to `crystallize-task` which in turn uses the `crystallize-worker` template** — the naming chain obscures that "doing something new" and "crystallizing" are coupled phases of a single workflow.

### 5.7 DOC/CODE DIVERGENCES

- No significant divergence on the `run.py` question: the validator (validate_skill.py:14-17) and spec-summary.md (line 54: "Include `run.py` when the skill has deterministic steps") agree that `scripts/run.py` is optional even for crystallized skills. The validator only checks `run.py`'s PEP 723 header *when the file is present* (validate_skill.py:67-74), and `validate_skill_test.py:87` asserts a crystallized skill with no `run.py` is valid. `crystallize-task/SKILL.md` success criteria (line 191) says "agentskills.io-compliant, `metadata.crystallized: true`" without claiming `run.py` is mandatory, consistent with the validator.

### 5.8 Recommended Canonical Term

**"crystallized skill"** for a skill with `metadata.crystallized: true`. **"hand-authored skill"** for others. The four lifecycle operations (`do-something-new`, `crystallize-task`, `heal-skill`, `update-skill`) are well-named. **"scenario"** for test scenarios (already the term in spec-summary.md). Avoid "test" for scenarios since they are ephemeral and not automated test files.

---

## 6. System-services Agent — the hidden primary agent (is_primary=true)

### 6.1 Canonical Definition

The **system-services agent** (also called "primary agent" or "services agent") is a dedicated mngr agent within each Minds workspace whose sole purpose is to run the bootstrap and background services (telegram, web, cloudflared, runtime-backup, etc.). It is permanently hidden from the UI agent list and protected from destruction.

Key properties:
- **Name**: `system-services` (constant across all workspaces; branch is `mngr/<host_name>` to avoid collision — see agent_creator.py:618-625)
- **Label**: `is_primary=true` (the authoritative signal; never env-var-based)
- **Label**: `workspace=<host_name>`
- **Command**: `sleep infinity && claude` — Claude never actually starts; the `&& claude` is unreachable, present only to satisfy `assemble_command`'s claude-shaped invariant (apps/minds/README.md:38)
- **Created by**: `_build_mngr_create_command()` in `agent_creator.py` (the `mngr create` command is assembled at lines 609-632, with the `is_primary=true` label appended at line 630; desktop client side during workspace creation)

### 6.2 Where `is_primary` Is Defined and Enforced

**Definition / stamping** (where `is_primary=true` is written):
- `apps/minds/imbue/minds/desktop_client/agent_creator.py:630` — the `mngr create` command includes `"is_primary=true"` as a label when creating the system-services agent
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/admin.py:546` — the ImbueCloud admin CLI also stamps `is_primary=true` when baking the pool host agent

**Enforcement** (where `is_primary=true` prevents actions):
1. **Destroy guard** — `FCT:apps/system_interface/imbue/system_interface/server.py:850-857`:
   - `_destroy_agent()` checks `agent_state.labels.get("is_primary") == "true"`
   - Returns HTTP 400 with detail naming the `is_primary` label
   - Test: `test_destroy_rejects_is_primary_agent` (server_test.py:866-885)

2. **Interrupt guard** — `FCT:apps/system_interface/imbue/system_interface/server.py:489-496`:
   - `_interrupt_agent_endpoint()` checks the same label
   - Returns HTTP 400
   - Test: `test_interrupt_agent_rejects_is_primary_agent` (server_test.py:330-351)

3. **Frontend filter** (hidden from UI agent list) — `FCT:frontend/src/models/AgentManager.ts:206`:
   - `return agent.labels?.is_primary === "true"` (a function that identifies primary agents)
   - Applied in `DockviewWorkspace.ts:477-482` to filter the visible agent list

**Discovery / lookup** (where `is_primary` is read to find the services agent):
- `apps/minds/imbue/minds/desktop_client/backend_resolver.py:732-743` — `list_known_workspace_ids()` filters for agents with both `workspace` and `is_primary` labels
- `apps/minds/imbue/minds/desktop_client/backend_resolver.py:745-762` — `list_active_workspace_ids()` same but excludes DESTROYED hosts
- `apps/minds/imbue/minds/desktop_client/forward_cli.py:91` — default filter for `mngr-forward`: `has(agent.labels.workspace) && has(agent.labels.is_primary)`

### 6.3 How It Is Hidden from UI

Three layers:
1. **Frontend data layer**: `AgentManager.ts:206` identifies primary agents; `DockviewWorkspace.ts:1774` strips chat panels pointing at the is_primary agent. The filter is applied at the data layer before any component consumes the list.
2. **Server-side guards**: destroy (HTTP 400) and interrupt (HTTP 400) endpoints reject requests for is_primary agents even if the frontend hides them (defense-in-depth for direct curl/scripted access)
3. **mngr-forward filter**: only is_primary agents get forwarded (so only the services agent hosts the system_interface web app)

### 6.4 Bootstrap Creation Flow

On first container boot:
1. The workspace creation (desktop client side) creates `system-services` with `is_primary=true` via `mngr create`
2. The bootstrap (inside the container) detects the primary agent is running
3. The bootstrap creates a real chat agent named after the host (via `mngr create`)
4. The bootstrap writes `CLAUDE_CONFIG_DIR` to the host env file (`$MNGR_HOST_DIR/env`) so all subsequent agents share the services agent's Claude config dir

Source: apps/minds/README.md:38, agent_discovery.py:77-117 (chain for resolving CLAUDE_CONFIG_DIR including the host-env fallback introduced for `use_env_config_dir=True`).

### 6.5 Competing / Multiple Definitions

The agent is referred to by multiple names across the codebase:

| Name | Where | Status |
|---|---|---|
| `system-services` | agent_creator.py:485-491 (constant comment), README.md:38, FCT CLAUDE.md | The mngr agent name (constant) |
| "primary agent" | apps/minds/README.md:38, docs/design.md:20 | General description |
| "services agent" | server.py:479, 838-840 | Used in error messages and comments |
| "the is_primary agent" | server_test.py:330,866, DockviewWorkspace.ts | Test/comment shorthand |
| `SYSTEM_SERVICES_AGENT_NAME` | agent_creator.py:46 (imported constant), backend_resolver.py:40 (definition) | Python constant for the name |

### 6.6 `SYSTEM_SERVICES_AGENT_NAME` Constant

Imported in `agent_creator.py:46` from `backend_resolver`:
```python
from imbue.minds.desktop_client.backend_resolver import SYSTEM_SERVICES_AGENT_NAME
```

The constant value is `"system-services"` (defined in `backend_resolver.py:40`; referenced in the agent_creator.py:485-491 constant comment and used to build the create address in `_build_mngr_create_command()`).

### 6.7 Ambiguities / Inconsistencies

1. **"primary" means two different things**: the `is_primary=true` label means "services agent, hidden from UI"; the "primary agent" in general conversation often means "the main chat agent the user is talking to." These are opposite agents in the same workspace.

2. **The command `sleep infinity && claude` is intentionally unreachable**: the `&& claude` part never executes. This is noted in README.md:37 as a workaround for `assemble_command`'s requirement. It is misleading — the agent's purpose is NOT to run Claude, but the command structure implies it.

3. **`is_primary` is a label (string "true"), not a typed boolean**: label values are always strings. The check `labels.get("is_primary") == "true"` (Python) and `agent.labels?.is_primary === "true"` (TypeScript) are correct but fragile — a label `is_primary=True` (capital T) or `is_primary=1` would pass the label filter at discovery but fail the equality check.

4. **The bootstrap agent name is `system-services` globally** but the branch is per-host (`mngr/<host_name>`). This means:
   - Mngr sees the agent as named `system-services` on each host
   - The branch distinguishes the per-workspace work history
   - On ImbueCloud, the pool host has a pre-baked `system-services` agent that the lease/adopt path hydrates; the create command passes `--reuse` so mngr's pre-flight expects the existing agent (agent_creator.py:634-657)

### 6.8 DOC/CODE DIVERGENCES

- **DOC/CODE DIVERGENCE**: `apps/minds/docs/design.md:20` says "the services agent is hidden from the UI agent list and the system_interface destroy endpoint refuses to tear it down." This is accurate for destroy, but does NOT mention the interrupt guard (HTTP 400 for interrupt too). The interrupt guard was added (server.py:470-515) but design.md was not updated.

- **DOC/CODE DIVERGENCE**: `apps/minds/UNABRIDGED_CHANGELOG.md:2121` says "the workspace_server `/api/agents/<id>/destroy` endpoint refuses to destroy `is_primary=true` agents" — accurate. But the changelog does not mention the `/api/agents/<id>/interrupt` endpoint also refusing is_primary agents. The interrupt guard exists in code (server.py:489-496) but was not prominent enough to be called out in changelog.

### 6.9 Recommended Canonical Term

**"services agent"** as the canonical term (it's what the error messages say and it describes the purpose). **"`is_primary` label"** as the label name. Avoid "primary agent" (confusing, since it collides with "the main chat agent" in UX language). The constant `SYSTEM_SERVICES_AGENT_NAME = "system-services"` is the correct identifier to use in code.

---

## Cross-Cutting Summary

### Headline Inconsistencies

1. **"task" is catastrophically overloaded**: it simultaneously means (a) a `tk create -t task` ticket type, (b) a `task.md` file handed to a worker, (c) the `launch-task` skill's slug, (d) a worker's bounded work unit, and (e) informally any bounded piece of work. No term is unambiguous.

2. **Worker = sub-agent = background agent**: three co-existing terms for the entity launched by `launch-task`. `launch-task/SKILL.md` uses "sub-agent" in its description/prose but "worker" in its scripts. The repo memory policy prefers "background agent." This is unresolved in the actual skill files.

3. **"step" means step record (a ticket), a progress step in a skill, and a numbered item in a list**: all three senses appear within the same CLAUDE.md context. The `-step-` ID segment is the only reliable disambiguation at the data layer.

4. **"primary agent" refers to opposite agents in different contexts**: the `is_primary=true` services agent (hidden, no Claude) vs. "the primary agent" as the main chat agent the user talks to. The README and design docs use both senses in close proximity.

5. **Reviews have two orthogonal meanings**: the `.reviewer` code-review gate (automated, CI) and the user-approval gates in worker flows (Gate 1/Gate 2 report mechanism). Both are called "review" in different contexts.

6. **TicketState (Python) parses a subset of the tk frontmatter schema**: `deps`, `links`, `type`, `priority`, `tags` are written by `tk` but invisible to the Python consumer. This creates a latent divergence risk if code consuming `TicketState` assumes it has the full schema.

7. **`scripts/run.py` is optional even for crystallized skills**: both the validator (validate_skill.py:14-17, with `validate_skill_test.py:87` asserting a crystallized pure-prose skill is valid) and spec-summary.md (line 54) agree. The validator only enforces a PEP 723 header on `run.py` when the file is present. A crystallized skill may be pure prose.
