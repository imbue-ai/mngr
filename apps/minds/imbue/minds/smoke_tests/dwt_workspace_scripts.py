"""The text the smoke suite runs inside a workspace to run default-workspace-template's CI suites there.

Nothing here runs in the suite's own process: all of it is executed in the workspace.
:mod:`imbue.minds.smoke_tests.dwt_suites` fills in the per-run parts.
"""

from typing import Final

OFFLOAD_CONFIG_GLOB: Final[str] = "offload-modal*.toml"
# Prints every offload config at the repo root as one JSON object, file name to text.
READ_OFFLOAD_CONFIGS_COMMAND: Final[str] = (
    "python3 -c 'import glob, json; print(json.dumps("
    '{path: open(path, encoding="utf-8").read() for path in sorted(glob.glob("' + OFFLOAD_CONFIG_GLOB + '"))}))'
    "'"
)

# The runner's fixed part. The variables above it are set per run; one ``run_group`` line per group follows it.
RUNNER_FUNCTIONS: Final[str] = r"""
# A fresh workspace installs its browser stack in env-converge, a one-shot
# supervisord program that does not hold up boot; the suites expect what it installs.
wait_for_env_converge() {
    local deadline status_line
    deadline=$(( $(date +%s) + env_converge_wait_seconds ))
    while :; do
        status_line=$(supervisorctl status env-converge 2>&1 || true)
        case "$status_line" in
            *RUNNING*|*STARTING*) ;;
            *) break ;;
        esac
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "env-converge was still running after ${env_converge_wait_seconds}s; running the suites anyway"
            break
        fi
        sleep 10
    done
    printf '%s\n' "$status_line" > "$results_dir/env-converge-status.txt"
}

# One run of the group, then reruns of only its failed tests (pytest's --lf)
# while tests fail and retries remain: offload's retry of a group's failures.
run_group() {
    local suite="$1" group="$2" retry_count="$3" command_line="$4"
    local group_dir="$results_dir/$suite/$group" attempt=1 exit_code
    mkdir -p "$group_dir"
    while :; do
        if [ "$attempt" -eq 1 ]; then set --; else set -- --lf --lfnf=none; fi
        echo "Running the $suite suite's $group group, attempt $attempt"
        bash -c "$command_line \"\$@\"" "$suite-$group" -o junit_family=xunit1 \
            --junitxml="$group_dir/attempt-$attempt.xml" "$@" > "$group_dir/attempt-$attempt.log" 2>&1 < /dev/null
        exit_code=$?
        echo "$exit_code" > "$group_dir/attempt-$attempt.exit"
        # pytest exits 1 exactly when tests failed, the one outcome a retry can change.
        if [ "$exit_code" -ne 1 ] || [ "$attempt" -gt "$retry_count" ]; then
            return 0
        fi
        attempt=$((attempt + 1))
    done
}

wait_for_env_converge
cd "$repo_root" || exit 1
"""
