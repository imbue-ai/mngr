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

`self-check` runs the offline asserts (persona loader, trial expansion, slug).
