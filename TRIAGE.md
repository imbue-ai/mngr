# Open GitHub issues triage

Living scratch notes. Last update: 2026-04-27.

Refresh issue states with `./scripts/check_triage_issues.sh` (parses this file for `#NNNN` refs).

## In progress (someone working on)

#1037, #1036, #1035, #1034, #1038, #1408, #1411, #1046, #1043

## Closed since first sweep (verified via gh)

#475, #491, #751, #1040, #1041, #1045, #1051, #1060, #1280, #1332

## Skipped from triage (not pursuing right now)

#1048, #1098, #1101, #1088, #1039, #1256, #1154, #1073, #1096, #1237, #1090

## Remaining to triage

| # | Title (short) | Status | Evidence | Tractability |
|---|---|---|---|---|
| #473.3 | `mngr --help` slow (~0.5s) | OPEN | Still 0.50s timed | Small |
| #473.7 | `--in docker` should hint to install plugin | OPEN | User adding hint to error message | Small |
| #473.11 | `--initial-message` missing from `c` synopsis | OPEN | User investigating | Small |
| #473.12 | `mngr c --help` too verbose | OPEN | Judgment call; superset of #473.11 | Small |
| #473.13 | Force-destroy when create fails late | PARTIAL | `--reuse --update` exists; late-failure case still leaks state | Medium (investigation) |
| #473.27 | `connect` interactive `--project` filter | OPEN | No `--project` flag in connect.py; user "will consider" | Medium |
| #473.28 | Return to `mngr connect` TUI after disconnect | OPEN | No re-entry mechanism; user "will consider" | Medium |
| #473.29 | Worktrees in `REPO_ROOT/.mngr/worktrees` | OPEN | `--worktree-base` exists; default unchanged; user "will see" | Small (change default) |
| #1095 | Host lock during agent start | PARTIAL | api/find.py:375 + cli/start.py:184 still unwrapped | Medium |
| #1089 | Warn on mid-file corrupt JSONL | OPEN | No position-aware handling | Medium |
| #1091 | `events_schema.jsonl` convention | OPEN | No schema files exist | Medium |
| #1087 | ConcurrencyGroup `check_interval` enforcement | OPEN | No check_interval param | Medium |
| #1092 | CG auto-teardown on thread crash | OPEN | No crash-teardown logic | Medium |
| #1099 | GC orphaned Docker containers/images | OPEN | No reconciliation in api/gc.py | Medium |
| #1106 | Auto-trigger GC on time elapsed | OPEN | No timestamp/interval logic | Medium |
| #1158 | GitHub Copilot CLI agent type | OPEN | No copilot plugin; reporter has draft PR | Medium |
| #1360 | ssh-relay threads spin 80% CPU | OPEN | ssh_tunnel.py:571 still uses `select.select` | Medium |
| #1049 | Stale host records after destroy+gc | OPEN | No repro test written | Medium (investigation) |
| #1102 | `schema.json` for event sources | OPEN | No schema files | XL, design |
| #1104 | GC for failed/partial hosts | OPEN | Design unresolved | Design |
| #1108 | Design error hierarchy | OPEN | Pure design, no decision | Design |
| #1002 | Stop hook 'No stderr output' blocks completion | OPEN | No fix; vague repro | Hard, needs repro |
| #476 | `list` RUNNING but agent waiting | OPEN | No repro info | Hard, needs repro |
| #522 | Modal `NotFoundError` on first install | OPEN | Truncated trace | Hard, insufficient info |
| #567 | `ReadTimeout` on docker plugin | OPEN | Single user, truncated | Hard, insufficient info |

## #473 megathread sub-status

Sub-items use bowei's original numbering from the megathread comment.

Resolved per maintainer comment (2026-04-27):
1, 2, 4, 5, 6, 8, 9, 10, 14, 15.a, 15.b, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 30

See the table above for the remaining 8 sub-items.

## Fix commits (for the closed-because-fixed)

| Issue | Commit |
|---|---|
| #1332 `--version` crash | `33a20d9c1` Fix mngr --version crash by using correct package name (imbue-mngr) |
| #1040 remove `--context` | `a8c5d2f8d` Remove rarely-used CLI flags to simplify mngr create and provision |
| #1045 duplicate agent name — early error | `e477fdc30` Prevent duplicate agent names on the same host + `dcc024372` Move duplicate name check inside lock |
| #1051 pygtail rotation | `9e5ddbb45` Add log_patterns to Pygtail so it finds timestamp-rotated files |
| #1280 parent-death watchdog | `880860a77` Add --daemonize/--no-daemonize to observe, events, and wait commands |
| #1041 trust_working_directory remote | `de2425378` Unify provisioning into generate-then-transfer pattern (added `should_trust_work_dir()`) |
