# Debugging E2E tests

## Test artifacts

Each e2e test produces artifacts in `.test_output/<timestamp>/<test_name>/`:

- **transcript.txt** -- a log of every command run during the test, with stdout, stderr, and exit codes
- **tutorial_block.txt** -- the original tutorial script block the test covers (if any)
- **\*.cast** -- asciinema recordings of tmux sessions launched during the test

You can view these in a browser by running the test output viewer:

```bash
uv run python -m imbue.mng.e2e.serve_test_output
# Open http://127.0.0.1:8742
```

To control artifact saving, use the `--mng-e2e-artifacts` flag:

```bash
just test path/to/test.py                                # default: always save artifacts
just test path/to/test.py --mng-e2e-artifacts=on-failure # only on failure
just test path/to/test.py --mng-e2e-artifacts=no         # never
```

## Keeping the test environment alive

When a test fails, it's often useful to poke around in the environment it created -- inspect the agents, check tmux sessions, run mng commands manually. The `--mng-e2e-keep-env` flag prevents the test teardown from destroying agents and killing the tmux server:

```bash
just test path/to/test.py::test_that_failed --mng-e2e-keep-env=on-failure
```

The three values are:

- `no` (default) -- always clean up after the test
- `on-failure` -- keep the environment only when the test fails
- `yes` -- always keep the environment (even on success)

Note: `--mng-e2e-artifacts` must be at least as broad as `--mng-e2e-keep-env` (e.g., you cannot use `--mng-e2e-artifacts=no` with `--mng-e2e-keep-env=yes`).

When the environment is kept, the test output includes all the env vars you need. Set them in your shell to interact with the test's agents and tmux sessions:

```bash
export MNG_HOST_DIR=/path/from/output
export MNG_PREFIX=mng_xxx-
export MNG_ROOT_NAME=mng-test-xxx
export TMUX_TMPDIR=/tmp/mng-e2e-tmux-xxx
unset TMUX
cd /path/to/cwd
```

### Listing agents

```bash
mng list
```

### Sending messages to agents

`mng message` sends a text message to a running agent without connecting interactively. This works on both local and remote agents:

```bash
mng message <agent_name> "What is your status?"
```

### Capturing agent output

`mng capture` takes a snapshot of an agent's current terminal output. This is useful for seeing what the agent is doing without attaching:

```bash
mng capture <agent_name>
```

### Connecting to an agent's tmux session

```bash
TMUX_TMPDIR=/tmp/mng-e2e-tmux-xxx tmux list-sessions
TMUX_TMPDIR=/tmp/mng-e2e-tmux-xxx tmux attach -t <session_name>
```

### Running commands on agents

```bash
mng exec <agent_name> 'ps aux'
mng exec <agent_name> 'cat /some/file'
```

### Cleaning up

When you're done debugging, run the destroy script that was saved alongside the test artifacts:

```bash
./libs/mng/imbue/mng/e2e/.test_output/<timestamp>/<test_name>/destroy-env
```

This destroys all agents and kills the isolated tmux server.
