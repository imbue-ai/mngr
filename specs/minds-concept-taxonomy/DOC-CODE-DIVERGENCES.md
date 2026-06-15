# Doc / code divergence register

Places where the documentation (`Minds_concepts.md`, docs/, READMEs, glossaries, style
guide, docstrings, changelogs) says something the **code does not do**. The code is
authoritative; each row cites where.

Severity:
- **HIGH** — the doc would lead you to wrong behavior or a wrong mental model of a
  security/identity boundary.
- **MED** — the doc misleads about structure or an outcome, but is unlikely to cause harm.
- **LOW** — imprecision, staleness, or undocumented-but-correct behavior.

| # | Sev | Concept | Doc says | Code does | Citation |
|---|---|---|---|---|---|
| 1 | **HIGH** | permissions storage | `latchkey-permissions.md:131-133`: opaque permissions file is replaced by a symlink to `~/.minds/agents/<agent_id>/latchkey_permissions.json` — i.e. **per-agent** isolation. | Permissions are keyed by **`host_id`**: `permissions_path_for_host()` → `<data_dir>/hosts/<host_id>/latchkey_permissions.json`. Multiple agents on one host **share one** permissions file. | `libs/mngr_latchkey/.../store.py:264-271` (verified) |
| 2 | MED | permissions path | `latchkey-permissions.md:122`: path is `~/.minds/latchkey/permissions/<uuid>.json`. | Real path inserts a plugin subdir: `<LATCHKEY_DIRECTORY>/mngr_latchkey/permissions/<uuid>.json` (`PLUGIN_DATA_SUBDIR_NAME="mngr_latchkey"`). | `store.py:65,82,309` |
| 3 | **HIGH** | permission outcomes | `Minds_concepts.md:50` (and `request_events.py` docstring): outcomes are "granted/denied/**failed**". | `RequestStatus` has only `GRANTED`, `DENIED`. No `FAILED`. | `apps/minds/.../request_events.py:47-51` (verified) |
| 4 | MED | permission requests source | `request_events.py:1-12` docstring: agents write request events to `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl`. | Since latchkey 2.9.0 requests stream from the gateway (`GET /permission-requests?follow=true`) via `PermissionRequestsConsumer`; the JSONL path is now only for *response* events. Docstring is stale. | `apps/minds/.../latchkey/permission_requests_consumer.py:6-9` |
| 5 | MED | data preferences | `UserDataPreference` enum implies `CONVENIENCE` and `PRIVACY` differ (import-much vs gather-minimal). | Only `CONTROL` is distinguished (`is_scan_requested`); `CONVENIENCE` and `PRIVACY` produce the identical scan/document — the distinction is a **no-op** today. | `apps/minds/.../onboarding.py:106-108` |
| 6 | MED | services agent guards | `apps/minds/docs/design.md:20` & `UNABRIDGED_CHANGELOG.md:1768`: the `is_primary` agent is protected from **destroy**. | Destroy **and interrupt** are both guarded (HTTP 400). The interrupt guard is undocumented in design.md/changelog. | `system_interface/server.py:489-496` |
| 7 | MED | layout inspect | `manage-layout/SKILL.md` presents `active_panel` as a panel. | `layout_inspect` returns `"active_panel": dockview.get("activeGroup")` — a **group id**, not a panel id. Naming bug. | `system_interface/layout_ops.py:407` (verified) |
| 8 | MED | browsers | `Minds_concepts.md:77` lists "browsers — tabs where you can have a (partially agent-controlled) browser" as an **existing** presentation concept. | No `BrowserPanel`/browser component/DOM-or-network control exists. Closest is an ad-hoc `url:<hash>` iframe navigable only via `replace-url`. Aspirational, not implemented. | `system_interface/frontend/.../DockviewWorkspace.ts:722-746` |
| 9 | LOW | host backups | `style_guide.md` test taxonomy (unit/integration/acceptance/release) omits **deployment tests** and **e2e** as categories, though both exist (`apps/minds/.../deployment_tests/`, `e2e_workspace_runner.py`). | The categories exist in code/dirs but are absent from the style guide. | `style_guide.md:1729-1909`; `apps/minds/.../deployment_tests/` |
| 10 | LOW | events | `style_guide.md:1400-1434` documents only the on-disk JSONL event log. | A second, in-memory **SSE event system** (`AgentEventQueues`, `BufferBehavior`) exists in `system_interface` and is undocumented in the style guide; no bridge to the log. | `system_interface/event_queues.py`, `events.py` |
| 11 | LOW | runtime backup naming | `runtime_backup/README.md` frames the orphan-branch history as a "fine-grained checkpoint". | The runner code only ever says "backup" (commit message `"runtime backup: ..."`); never "version"/"checkpoint" in code. Term mismatch. | `FCT/libs/runtime_backup/.../runner.py:127` |
| 12 | LOW | host name | (no doc) `CertifiedHostData.host_name` is silently normalized for legacy values (`"@local"`,`"local"`,`"unknown-host-at-local"` → `"localhost"`). | Behavior is real but undocumented; callers reading `host_name` may not get what was set at creation. | `libs/mngr/.../interfaces/data_types.py:282-304` |
| 13 | LOW | stop reason | `CertifiedHostData.stop_reason` docstring is the *only* spec for its values (`"PAUSED"`/`"STOPPED"`/`None`); it is an untyped `str|None`. | No type/enum enforces the contract — a writer using a different string silently violates it. | `libs/mngr/.../interfaces/data_types.py:345` |
| 14 | LOW | runtime state | `FCT/CLAUDE.md:16`: "State directories live under `runtime/<feature>/`". | Not all state is a directory (`runtime/last-restic-prune` is a file); and `host_backup`'s `get_events_dir()` can resolve `events/backup` **outside** `runtime/`. The convention has unstated exceptions. | `FCT/libs/host_backup/.../config.py:222-227` |
| 15 | LOW | workspace label | `Minds_concepts.md:30`: "labeled `workspace=`" implies a truthy value. | Discovery checks key **presence** only (`"workspace" in agent.labels`); production sets `workspace=<host_name>`, but some tests set `workspace="true"`. Imprecise doc, harmless code. | `apps/minds/.../backend_resolver.py:742`; `agent_creator.py:617` |
| 16 | LOW | model registry | `litellm_proxy/config.yaml` comment: model list "MUST stay in sync with apps/modal_litellm/app.py". | No automated enforcement of the sync; manual only. | `litellm_proxy/config.yaml` |
| 17 | LOW | disallowed vs guarded tools | FCT `--disallowed-tools` disables `TaskCreate/TaskList/TaskUpdate`; `claude_require_steps_pretool.sh` *also* exempts those same tools from step checks. | Harmless redundancy (the exemption is moot for disabled tools) but reflects uncoordinated maintenance. | `.mngr/settings.toml`; `scripts/claude_require_steps_pretool.sh` |
| 18 | LOW | git remote | mngr push/pull is described as a "remote" operation. | `_build_ssh_git_url()` passes the URL positionally to `git push`; **no named remote** is stored in `.git/config`. Operators inspecting `.git/config` won't see it. | `libs/mngr/.../api/git.py` |

## Notes on the source `Minds_concepts.md`

Rows 3 and 8 are divergences in the *concepts doc itself* (it lists "failed" as a
permission outcome and "browsers" as an existing concept). These should be corrected in
`Minds_concepts.md` when it is revised: drop "failed", and move "browsers" to the
"Concepts we might want someday" section (or annotate it as not-yet-implemented).

## Suggested follow-ups

- The two **HIGH** rows (1, 3) touch the security/identity boundary and the permission
  outcome contract — fix the docs first (cheap) and consider whether per-host permission
  sharing (row 1) is the intended design or a latent isolation gap.
- Row 5 is behavior the docs promise but the code doesn't deliver
  (`CONVENIENCE`/`PRIVACY` no-op) — decide whether to fix the code or the docs.
- Rows 9, 10 are style-guide gaps — additive doc fixes.

A previously-listed divergence (crystallized skills: validator allegedly *requires*
`scripts/run.py`) has been **dropped**: `validate_skill.py` now states run.py is optional
even for crystallized skills (`validate_skill.py:16-17,68`) and `validate_skill_test.py`
asserts it, so doc and code agree.
