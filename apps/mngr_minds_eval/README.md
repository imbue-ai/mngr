# mngr-minds-eval

`minds-evals` — a harness for running persona-based evals against Minds.

Launch a batch of persona cases as **self-completing** Modal workspaces: each sandbox drives its own
multi-turn conversation, snapshots `/mngr` to S3 after each turn (restic), and uploads its
transcript at the end. Results are read back from S3, so the launching machine does not need to
stay on. Afterwards, `visit-batch` rebuilds the **exact computer** the batch ran on — a Docker box
running the real Minds desktop app, streamed to your browser — and you open the batch's workspaces
as windows, natively.

## How it fits together

- **The box** — a Docker container that is a full Minds computer, pinned to an exact mngr SHA.
  One image, two run modes:
  - *headless*: `minds run` serving the Minds HTTP API. Used by `launch` to create a batch's
    workspaces, then torn down (the workspaces self-complete on Modal).
  - *desktop*: the real Minds Electron app on a virtual display (Xvfb + openbox), streamed to your
    browser via noVNC. Used by `visit-batch`: one published port, you enter a real desktop and use
    Minds natively — multiple workspace windows and all. No host-side tunnels.
- **Workspaces** — always Modal sandboxes. Never run in the box.
- **The eval name IS the batch** — unique, hard requirement. It names the S3 prefix and the
  batch's own Modal env (`minds-staging-<name>`, via the Modal provider's `user_id`); `launch`
  preflights BOTH and fails out if either already exists. A box only ever discovers its own
  batch's workspaces, so discovery stays small and fast. The env, the mngr SHA, and the branch
  are recorded in the batch's S3 config — which is what makes `visit-batch` exact.
- **Shared SSH access** — every box pins one mngr profile (`evaluator`) and mounts one shared Modal
  SSH keypair (persisted at `~/.minds-eval/modal-profile/`, seeded by the first box), so a visit
  box can open workspaces a launch box created.

## Setup

One-time: an S3 bucket and a bucket-scoped IAM key at `~/.minds-eval/aws.env`. See [SETUP.md](SETUP.md).

## Commands

```
# run an eval batch (one self-completing workspace per case) from a single config file
ANTHROPIC_API_KEY=sk-ant-... minds-evals launch --config eval-config.json

# status, straight from S3 -- no box, works any time from anywhere
minds-evals list-batches
minds-evals inspect combined

# score a finished batch (S3 + Anthropic only, no box); writes results back to S3
ANTHROPIC_API_KEY=sk-ant-... minds-evals evaluate combined

# rebuild the batch's exact Minds computer and enter its desktop in your browser
minds-evals visit-batch combined

# dev utility: a desktop box on any mngr branch tip (Modal env minds-staging-<user-id>)
minds-evals box --mngr-branch main --user-id dev
```

`launch` first verifies the eval name is unused (no such S3 batch, no such Modal env — it fails
out otherwise), then builds/boots a headless box for the batch (pinned to the branch tip SHA),
creates the workspaces inside it, then removes the box — the workspaces self-run on Modal and write to S3.
`visit-batch` reads the batch's recorded `(mngr_sha, modal env)` from S3, boots a desktop box that
IS that computer, and prints a noVNC URL. `list-batches`/`inspect`/`evaluate` only read S3.

## Eval config (`--config`)

A single json, stored verbatim in S3 as the batch config (plus `created_at`, `restic_password`,
`mngr_sha`, `modal_user_id`, `modal_env`). See `eval-config.json`:

```json
{
  "name": "combined",
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

`name` must be lowercase letters/digits/dashes (max 40) and **globally unique** — it is the S3
prefix and the Modal env name. `fct_branch`/`fct_repo` are optional (default the
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
**partial** transcript. Snapshots are captured to S3 per turn.

## Evaluating a finished batch (`evaluate`)

`minds-evals evaluate <batch>` reads the batch from S3 (no box, no Modal), then scores every
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

1. Reads the batch's `config.json` from S3 → the mngr branch, the **exact SHA**, and the batch's
   Modal env.
2. Builds/boots a **desktop box** at that SHA with that env (reused if already running — the
   container name encodes env + SHA + mode).
3. Prints a noVNC URL. Open it: a real Linux desktop running the actual Minds app, whose discovery
   sees exactly that batch's workspaces. Open them as windows, read the conversations, poke around.

The Minds app inside the box reaches its workspaces itself (mngr forward on the container's own
loopback) — nothing is tunnelled through your host. When you're done: `docker rm -f <box>` (the
URL printout includes the exact command).

## S3 layout

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
box.py             the box: build at an exact SHA, run headless or desktop, per-batch Modal env
minds_client.py    the Minds create API (POST + poll) -- used by launch
launch.py          batch: prep clone (+ vendor mngr + slot test_case_metadata.json), create per case
workspace.py       create one Modal workspace (build_payload + create_workspace) -- the create path
status.py          list-batches / inspect / case_report (S3 reads only)
evaluate.py        evaluate: pull transcripts, score (avg_word_count + LLM scores), write to S3
anthropic_call.py  one plain Anthropic Messages call (the LLM-graded evals)
s3_store.py        S3 layout, creds file, batch/case prefixes
docker/            Dockerfile + entrypoint.sh (one image; headless `minds run` or Xvfb+noVNC desktop)
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
