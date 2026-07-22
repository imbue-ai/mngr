# mngr-minds-eval

`minds-evals` — a harness for running persona-based evals against Minds.

Launch a batch of persona cases as **self-completing** Modal workspaces: each sandbox drives its own
multi-turn conversation, snapshots `/mngr` to R2 after each turn (restic), and uploads its
transcript at the end. Results are read back from R2, so the launching machine does not need to
stay on. Afterwards, `visit-batch` rebuilds the **exact computer** the batch ran on — a Modal
sandbox running the real Minds desktop app, streamed to your browser — and you open the batch's
workspaces as windows, natively. **Nothing runs on your machine**: no Docker, no local builds, no
port mappings — the CLI only makes API calls and prints https URLs.

## How it fits together

- **The box** — a **Modal sandbox** that is a full Minds computer, pinned to an exact mngr SHA:
  the real Minds app on a virtual desktop (Xvfb + openbox), streamed to your browser via noVNC
  through Modal's encrypted tunnel — one `https://…modal.host` URL, usable from any machine, **no
  auth** (the URL's randomness is the only lock). The image is built from `docker/Dockerfile` on
  Modal's builders (cached per SHA). `launch` creates the batch's workspaces *inside* that computer
  (the CLI discovers the app's API from within the sandbox) and leaves it running for you to watch;
  `visit-batch` finds the same computer again by tag, or reboots it. Boxes self-terminate after 8h
  (they bill while alive); `stop <name>` kills one early — the workspaces live on regardless.
- **Workspaces** — always Modal sandboxes. Never run in the box.
- **The eval name IS the batch** — unique, hard requirement. It names the R2 prefix and the
  batch's own Modal env (`minds-staging-<name>`, via the Modal provider's `user_id`); `launch`
  preflights BOTH and fails out if either already exists. A box only ever discovers its own
  batch's workspaces, so discovery stays small and fast. The env, the mngr SHA, and the branch
  are recorded in the batch's R2 config — which is what makes `visit-batch` exact.
- **Shared SSH access** — every box pins one mngr profile (`evaluator`) and mounts one shared Modal
  SSH keypair (persisted at `~/.minds-eval/modal-profile/`, seeded by the first box), so a visit
  box can open workspaces a launch box created.

## Setup

One-time: run `./setup-r2.sh` -- it creates an R2 bucket + scoped key and writes `~/.minds-eval/r2.env`. See [SETUP.md](SETUP.md).

## Commands

```
# run an eval batch (one self-completing workspace per case): a unique name + a config template
ANTHROPIC_API_KEY=sk-ant-... minds-evals launch combined --config eval-config.json

# status, straight from R2 -- no box, works any time from anywhere
minds-evals list-batches
minds-evals inspect combined

# score a finished batch (R2 + Anthropic only, no box); writes results back to R2
ANTHROPIC_API_KEY=sk-ant-... minds-evals evaluate combined

# the batch's exact Minds computer, in your browser
minds-evals visit-batch combined

# terminate the batch's box sandbox early (its workspaces live on)
minds-evals stop combined

# dev utility: a desktop box on any mngr branch tip (Modal env minds-staging-<user-id>);
# add --dwt-link/--dwt-branch to also create one workspace in it (needs ANTHROPIC_API_KEY)
minds-evals box --mngr-branch main --user-id minh
ANTHROPIC_API_KEY=... minds-evals box --mngr-branch main --user-id minh \
    --dwt-link https://github.com/imbue-ai/default-workspace-template.git --dwt-branch main
```

`launch` first verifies the eval name is unused (no such R2 batch, no such Modal env — it fails
out otherwise), then builds/boots the batch's computer (pinned to the branch tip SHA), creates the
workspaces inside it, and prints its desktop URL — enter it to watch the batch run; the workspaces
self-run on Modal and write to R2 regardless. `visit-batch` reuses that same computer by name if it
is still up, or reads `(mngr_sha, modal env)` from R2 and reboots it exactly. `list-batches`/
`inspect`/`evaluate` only read R2. Remove a box any time with `docker rm -f <box>`.

## Eval config (`--config`)

A reusable **template** — the batch name is given on the command line, not in the file. Stored
verbatim in R2 as the batch config (plus `name`, `created_at`, `restic_password`, `mngr_sha`,
`modal_user_id`, `modal_env`). See `eval-config.json`:

```json
{
  "mngr_branch": "main",
  "fct_branch": "minds-eval-autosend",
  "timeout_seconds": 3600,
  "personas": [
    {"id": "todo-app", "persona": "...", "prompts": ["Build me ...", "Sounds good.", "DECIDE_FROM_PERSONA"]}
  ]
}
```

Each case's `prompts` array is the conversation, one entry per turn — so different cases can run
different numbers of turns. Each entry is either:

- a **literal string** — sent to the agent verbatim (e.g. the opening ask, or `"Sounds good."`); or
- **`DECIDE_FROM_PERSONA`** — the in-sandbox worker role-plays the client: it feeds the
  transcript-so-far + the case's `persona` to the Anthropic API (using the key `launch` was given)
  and sends back a short casual reply. It cannot be the first entry (nothing to decide from yet).

The launch-time `name` must be lowercase letters/digits/dashes (max 40) and **globally unique** —
it is the R2 prefix and the Modal env name. `fct_branch`/`fct_repo` are optional (default the
workspace-template branch that carries the eval worker). `fct_branch` must carry the worker or the sandbox boots but never self-runs.
`timeout_seconds` is optional (default 3600 = 1h): a per-case wall-clock budget — a run that
exceeds it self-terminates.

## Turn logic (N = `len(prompts)`)

| turn | the in-sandbox worker does |
|---|---|
| 1 | send `prompts[0]` (a literal — the opening ask) |
| 2 .. N | `restic backup /mngr --tag post_message_<k>`, then send `prompts[k]` (literal, or a role-played reply for `DECIDE_FROM_PERSONA`) |
| after N | upload the full transcript, mark `finished`, exit |

Each turn writes `state.json` (`waits_done` / `num_turns` / `test_state` + timing: `started_at`,
`elapsed_seconds`, `timeout_seconds`, `timed_out`). `test_state` is `ongoing` while running,
`finished` on success, or `timed_out` if the case exceeded its budget — distinct from `ongoing`, so
a stalled/crashed run and a timed-out one are told apart. A timed-out case still uploads its
**partial** transcript. Snapshots are captured to R2 per turn.

## Evaluating a finished batch (`evaluate`)

`minds-evals evaluate <batch>` reads the batch from R2 (no box, no Modal), then scores every
**finished** case in parallel and writes results back, overwriting each case's result (a failed
re-run leaves the prior good results intact — there is no destructive pre-delete). Cases that
aren't finished (still running, **timed out**, or whose eval errors) show as `N/A` rows, called out
distinctly in the footnote, and are left out of the batch average. The per-case outputs are:

- `avg_word_count` — average words per agent turn, from the transcript.
- `conciseness_score` / `nontechnical_language_score` / `proactive_score` — three 1-10 scores from
  one Anthropic call per case (needs `ANTHROPIC_API_KEY`).

Per-case results land in `<case>/case_eval_results.json`, the batch average in
`<batch>/batch_eval_results.json`, and a table (rows = cases, columns = the keys, plus a
batch-average row) is printed. Add a new evaluation by appending a function to `EVALUATIONS` in
`evaluate.py`.

## Visiting a batch (`visit-batch`)

`visit-batch <batch>` is "walk back into the machine that ran this batch":

1. Reads the batch's `config.json` from R2 → the mngr branch, the **exact SHA**, and the batch's
   Modal env.
2. Builds/boots the batch's box at that SHA with that env (reused if already running — the
   container name encodes env + SHA).
3. Prints a noVNC URL. Open it: a real Linux desktop running the actual Minds app, whose discovery
   sees exactly that batch's workspaces. Open them as windows, read the conversations, poke around.

The Minds app inside the box reaches its workspaces itself (mngr forward on the sandbox's own
loopback) — nothing touches your machine. When you're done: `minds-evals stop <batch>` (or just let
the 8h timeout reap it).

## R2 layout

```
<name>/                              batch (the unique eval name)
  config.json                        the eval config verbatim + created_at + restic_password
                                     + mngr_sha + modal_user_id + modal_env
  <name>_<case_id>/
    state.json                       written by the worker each turn
    artifacts/full_transcript.jsonl  written by the worker on the final turn
    restic/                          the case's restic repo (tagged /mngr snapshots)
```

## Structure

```
main.py            argparse dispatch; re-invokes launch inside the headless box
box.py             the box: a Modal sandbox (image built on Modal from docker/Dockerfile) per batch env
minds_client.py    the Minds create API (POST + poll) -- used by launch
launch.py          batch: prep clone (+ vendor mngr + slot test_case_metadata.json), create per case
workspace.py       create one Modal workspace (build_payload + create_workspace) -- the create path
status.py          list-batches / inspect / case_report (R2 reads only)
evaluate.py        evaluate: pull transcripts, score (avg_word_count + LLM scores), write to R2
anthropic_call.py  one plain Anthropic Messages call (the LLM-graded evals)
s3_store.py        R2 layout, creds file, batch/case prefixes
docker/            Dockerfile + entrypoint.sh -- the box IMAGE SOURCE, built on Modal's builders
```

## Notes

- Snapshots use restic (deduped, encrypted, incremental). The worker drives restic itself with
  credentials + repo + password slotted into each clone's `scripts/test_case_metadata.json`
  (backup_provider is `configure_later`), because minds' `api_key` backup provider does not
  reliably land a `restic.env` inside a Modal sandbox.
- The eval worker lives on the FCT `minds-eval-autosend` branch and **no-ops unless
  `scripts/test_case_metadata.json` is present**, so normal workspaces on that branch are unaffected.
- Old batches (launched before per-batch envs) lack `modal_user_id` in their config and cannot be
  visited; relaunch them.
- Modal envs accumulate one per batch. Wipe one manually with
  `uv run python scripts/modal_nuke.py -e <modal_env> --force` (run with `TERM=dumb`; the env name
  is in the batch's config.json).
- The sandbox timeout is 3h (set in the FCT template); the per-case eval budget (default 1h) is
  enforced by the worker inside it.
