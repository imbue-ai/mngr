# Synthetic base: `mngr/agent-mixins-base`

A purpose-built, minimal base branch for the **agent capability mixins** work (see
`specs/agent-plugin-parity/capability-mixins.md`). It exists so that work sits on exactly its
prerequisites -- the agent-plugin / agent-capability branches that were in `ev/main` but not
yet in `main` -- without the unrelated work also carried by `ev/main` (cloud providers, dev
tooling). It is a throwaway integration base, not a long-lived branch: once these feature
branches land in `main` on their own, rebuild or retire it.

## Composition

`origin/main`, plus these feature branches merged in (each relevant because it touches the
agent plugins or a tracked capability):

| Branch | Why it's relevant |
|---|---|
| `mngr/catalyst-waiting` | Shared common-transcript shell lib (convert-lock + flush); transcript capability |
| `mngr/waiting-reason` | Hoists `WaitingReason` + classifier into core; the `waiting_reason` capability |
| `mngr/opencode-waiting-reason` | Adopts shared `waiting_reason` in opencode (contains `waiting-reason`) |
| `mngr/agy-preserve` | Session-preservation-on-destroy across the plugins; a tracked capability |
| `mngr/agents-usage` | Per-harness usage tracking (token/cost) + transcript converter extraction; the usage capability |

`waiting-reason` is not merged directly -- it is already contained in `opencode-waiting-reason`.

## Deliberately excluded

| Branch | Why excluded |
|---|---|
| `mngr/azure`, `mngr/gcp`, `mngr/aws-stop` | Cloud host providers -- unrelated to agent-type plugins |
| `mngr/separate-snapshots(-base)` | Cloud *VM*-snapshot work (drags in azure/aws/gcp); not the agent streaming-snapshot capability despite the name |
| `mngr/changelog-enforcement` | CI/dev tooling |
| `mngr/test-hang` | Cleanup-lifecycle infra, not a capability |
| `mngr/capture-any-window` | Window-capture infra, not a tracked capability (excluded by choice) |
| `mngr/usage-filter-by-age` | Already merged to `main`; comes in for free |
| `mngr/agy-statusline` | Already contained in `mngr/agents-usage` |

## How it was built

From `origin/main`, the branches were merged in dependency order (`catalyst-waiting`,
`opencode-waiting-reason`, `agy-preserve`, `agents-usage`). Conflicts were resolved by keeping
both sides:

- `mngr_codex` / `mngr_opencode` `plugin.py` + `plugin_test.py`: `agy-preserve`
  (session-preservation) vs `opencode-waiting-reason` (`waiting_reason`) added disjoint imports
  and test sections -- both kept.
- `mngr_claude/.../common_transcript.sh`: `catalyst-waiting`'s convert-lock and `agents-usage`'s
  before/after line counting both kept (acquire lock, then count).
- `mngr_schedule/.../test_cli.py`: kept the richer `@pytest.mark.flaky` + `@pytest.mark.timeout(30)`
  variant over the bare-timeout one.
