# mngr-minds-eval

Eval harness for Minds, run **inside** the minds-box container.

## prepare-test-clones

Given a personas file and an FCT branch, clone that branch once per `persona x trial` and slot
each persona's config into `scripts/first_command.json` (committed, so it ships to the sandbox).
That's the whole job for now -- creating a Modal workspace off each prepared clone comes later.

```
uv run --package mngr-minds-eval mngr-minds-eval prepare-test-clones \
    /work/personas.json --fct-branch minds-eval-autosend -n 1
```

- `personas` -- a list (or `{"personas": [...]}`) of `{"id", "persona", "first_prompt"}`. Only
  `first_prompt` is required; `id` is slugified into the clone/workspace name.
- `-n/--trials` -- clones to prepare per persona (default 1).
- `--fct-repo` / `--fct-branch` -- the FCT source (default the public autosend branch).
- `--clones-dir` (default `/work/clones`), `--base-dir` (default `/work/eval-base`).

Each prepared clone lands at `/work/clones/<id>` with the persona slotted and committed. The
in-sandbox `chat_watcher` (on the FCT branch) later delivers that persona's `first_prompt` as the
user, once the workspace's agent goes idle.

## launch-workspaces

Create a Modal workspace for every prepared clone under `--clones-dir` -- automating the create
form, field for field: **Modal** compute, **API_KEY** provider (key from `$ANTHROPIC_API_KEY`),
backup **configure-later**, and an **empty branch** (a local clone is already on the right commit;
passing a branch trips mngr's `checkout_branch(FETCH_HEAD)`). Each workspace is named
`EVAL-<eval-set>-CASE-<persona>`. Runs all creates in parallel.

```
ANTHROPIC_API_KEY=sk-ant-... \
  uv run --package mngr-minds-eval mngr-minds-eval launch-workspaces --eval-set smoke-test
```

## retrieve-test-results

For every `EVAL-<set>-CASE-<persona>` workspace, read the in-sandbox `/mngr/eval_state.json` and,
for finished cases, pull the Claude transcript. Uses `mngr rsync` (state file) + `mngr transcript`
(conversation) -- the SFTP/rsync-backed transports, not the unreliable `mngr exec`.

```
uv run --package mngr-minds-eval mngr-minds-eval retrieve-test-results --eval-set smoke-test -o ./results
```

Per case it reports: **unreachable** (machine not accessible), **no_state** (test not started),
**ongoing** (+ waits-processed count), or **finished** → writes `<persona>.jsonl`. Also writes
`summary.json`. Non-Modal providers are disabled for the mngr calls (only Modal works in the box).

`self-check` runs the offline asserts (persona loader, trial expansion, slug, launch payload,
name matcher, error classifier).
