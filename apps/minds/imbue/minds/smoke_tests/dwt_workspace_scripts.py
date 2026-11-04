"""The text the smoke suite runs inside a workspace to run default-workspace-template's CI suites there.

Nothing here runs in the suite's own process: it is the fixed part of the bash
runner, and the shell and Python one-liners that read the template's offload
configs, start the runner, and look at it, all executed in the workspace.
:mod:`imbue.minds.smoke_tests.dwt_suites` fills in the per-run parts.
"""

from typing import Final

# What the runner leaves directly under its results directory, as its text below names them: its pid, its own
# output, and ``done`` once it has ended, however it ended.
RUNNER_DONE_FILE_NAME: Final[str] = "done"
RUNNER_PID_FILE_NAME: Final[str] = "runner.pid"
RUNNER_LOG_FILE_NAME: Final[str] = "runner.log"

OFFLOAD_CONFIG_GLOB: Final[str] = "offload-modal*.toml"
# Prints every offload config at the repo root as one JSON object, file name to text.
READ_OFFLOAD_CONFIGS_COMMAND: Final[str] = (
    "python3 -c 'import glob, json; print(json.dumps("
    '{path: open(path, encoding="utf-8").read() for path in sorted(glob.glob("' + OFFLOAD_CONFIG_GLOB + '"))}))'
    "'"
)

# Starts the runner in a session of its own, so it outlives the command that started it and can be stopped
# whole, and gives it nothing to hold the command's output open with.
DETACH_PROGRAM: Final[str] = (
    "import subprocess, sys; "
    "subprocess.Popen(['bash', sys.argv[1]], start_new_session=True, stdin=subprocess.DEVNULL, "
    "stdout=open(sys.argv[2], 'wb'), stderr=subprocess.STDOUT)"
)
# Prints the runner's state, then the last line it logged.
RUNNER_STATE_COMMAND_TEMPLATE: Final[str] = (
    "if [ -e {done} ]; then echo FINISHED; "
    "elif [ ! -s {pid} ]; then echo STARTING; "
    'elif kill -0 "$(cat {pid})" 2>/dev/null; then echo RUNNING; '
    "else echo VANISHED; fi; "
    "tail -n 1 {log} 2>/dev/null || true"
)

# The runner's fixed part. The variables above it are set per run; one ``run_group`` line per group follows it.
RUNNER_FUNCTIONS: Final[str] = r"""
echo "$$" > "$results_dir/runner.pid"
trap 'date -u +%Y-%m-%dT%H:%M:%SZ > "$results_dir/done"' EXIT

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
