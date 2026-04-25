# Open GitHub issues triage (2026-04-24)

Scratch notes. Delete after closing the stale issues listed below.

## Already fixed in code — close on GitHub

| Issue | Fix commit | Date |
|---|---|---|
| #1332 `mngr --version` crash | `33a20d9c1` Fix mngr --version crash by using correct package name (imbue-mngr) | 2026-03-30 |
| #1040 remove `--context` flag | `a8c5d2f8d` Remove rarely-used CLI flags to simplify mngr create and provision | 2026-04-08 |
| #1045 duplicate agent name — early error | `e477fdc30` Prevent duplicate agent names on the same host; `dcc024372` Move duplicate name check inside lock and add rename guard | 2026-03-26 |
| #1051 pygtail rotation | `9e5ddbb45` Add log_patterns to Pygtail so it finds timestamp-rotated files | 2026-04-23 |
| #1280 parent-death watchdog | `880860a77` Add --daemonize/--no-daemonize to observe, events, and wait commands | 2026-04-22 |

## Partially fixed — verify before closing

- **#1041** — `de2425378` added `should_trust_work_dir()`; gates on `ctx.is_unattended`, not `host.is_local`. Likely fixes the symptom via the unattended path. Ask reporter to retest.
- **#1088** — `dd7b306c1` fixed `pre_readers.py` (TOML → raise); `loader.py:264` gone. `hosts/host.py:1348` still catches `JSONDecodeError` and warns on agent reference files. No ratchet yet. Leave open.
- **#1095** — create flow wrapped in `host.lock_cooperatively()`. Still unprotected: `api/find.py:375`, `cli/start.py:184`. Leave open.
- **#1096** — `logger.exception` cut to 1 in `libs/mngr` (`api/gc.py:806`) and 3 in `apps/minds_workspace_server/agent_manager.py`. Audit incomplete, no ratchet. Leave open.

## Still genuinely open (code unchanged)

Bugs: #1002, #1043, #1046, #1048 (`strict=False` only downgrades to warning), #1049, #1060, #1073, #1089, #1090, #1099, #1154, #1237, #1360, #1408, #1411

Refactors: #1034, #1035, #1036, #1037, #1038, #1039 (only `HookDefinition` + `EnvVarName` normalize; no general field-name normalization)

Enhancements/design: #1087, #1091, #1092, #1098, #1101 (`list.md` exists but lacks `age`/`runtime`/`idle`), #1102, #1104, #1106, #1108, #1158, #1256

## Cannot triage without more info

#473 (split megathread first), #475, #476, #491, #522, #567, #751
