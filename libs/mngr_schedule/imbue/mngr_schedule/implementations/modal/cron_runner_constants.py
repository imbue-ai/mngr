# Shared constants between cron_runner.py (runs inside the Modal container)
# and its drift-detection tests.
#
# cron_runner.py cannot import anything from imbue.* that would pull in a
# 3rd-party dependency, because Modal needs to load the module at both
# deploy time and runtime without extra packaging. This module is the one
# exception: it is pure-stdlib, its ancestor `__init__.py` files are all
# empty (imbue is a namespace package, and mngr_schedule's inits are blank
# per the project's "leave __init__.py blank" rule), so importing from
# here triggers zero user-code execution and drags in no 3rd-party deps.
#
# The constants below mirror values that are also defined in imbue.* code
# (e.g. `AgentLifecycleState`, `VerifyMode`). cron_runner.py cannot import
# those enums directly, so it would otherwise have to inline the literals
# and silently drift. Keeping the mirror here lets the test file import
# both the mirror and the authoritative enum, and assert they match.


# Lifecycle states (as reported by `mngr list --format json`) that indicate
# the agent is still actively running. Any other state is treated as terminal
# by the in-container full-verify poll loop. Mirror of the "running" subset of
# `imbue.mngr.primitives.AgentLifecycleState`; drift is caught by
# cron_runner_constants_test.py.
RUNNING_STATES: frozenset[str] = frozenset({"RUNNING", "WAITING", "REPLACED", "RUNNING_UNKNOWN_AGENT_TYPE"})

# Accepted values for the `verify_mode` argument of `run_scheduled_trigger`.
# Mirror of `imbue.mngr_schedule.data_types.VerifyMode` values (lowercased);
# drift is caught by cron_runner_constants_test.py.
VALID_VERIFY_MODES: frozenset[str] = frozenset({"none", "quick", "full"})

# Sentinel value returned by the in-container lifecycle-state lookup when the
# named agent is not present in `mngr list` output. Deliberately distinct from
# any real AgentLifecycleState value so the deploy-side verifier can tell
# "agent vanished" apart from "agent reached an unexpected terminal state".
# Must not collide with any AgentLifecycleState value; enforced by the drift
# test.
AGENT_MISSING_STATE: str = "MISSING"

# Sentinel line prefix used by cron_runner.run_scheduled_trigger to emit a
# single-line JSON result at the end of a verify invocation, and matched by
# verification._SENTINEL_PATTERN to parse that result back on the deploying
# machine. Both sides must use the exact same literal; sharing the constant
# here eliminates the risk of silent drift between the emitter and the parser.
RESULT_SENTINEL: str = "__MNGR_SCHEDULE_VERIFY__"
