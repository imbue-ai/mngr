# mngr-minds-eval

`minds-evals` — a harness for running persona-based evals against Minds.

Launch a batch of persona cases as **self-completing** Modal workspaces: each sandbox drives its own
multi-turn conversation, snapshots `/mngr` to S3 after each turn (restic), and uploads its
transcript at the end. Results are read back from S3, so the launching machine does not need to
stay on and there is nothing to poll.

## How it fits together

- **The box** — a local Docker container running headless Minds, named `minds-box-<branch>-<sha>`.
  It is the mngr isolation and the thing that talks to Modal + serves you the UI on localhost. The
  container name encodes the exact mngr SHA, so a running box of that name *is* that mngr — reuse is
  idempotent and never stale. All commands key the box on `--mngr-branch`.
- **Workspaces** — always Modal sandboxes. Never run in the box.
- **Modal env** — one shared env `minds-staging-evaluator` for ALL eval workspaces (any branch/SHA),
  so `clean` has a single place to wipe. The box stays versioned; only the env is shared. S3 (keyed
  by eval name) is the real result store; sandboxes are told apart by host name + the S3 batch.
- **Shared SSH access** — Modal sandboxes are reached over SSH tunnels (`mngr forward`), and mngr
  normally rolls a *random per-box* SSH keypair, so only the box that created a workspace could open
  it. Every eval box instead pins one mngr profile (`evaluator`) and mounts one shared keypair
  (persisted at `~/.minds-eval/modal-profile/`, seeded by the first box), so **any box can open or
  forward into any workspace** in the shared env. Applies to workspaces created after a box is
  (re)built with this; existing ones keep their old key.

## Setup

One-time: an S3 bucket and a bucket-scoped IAM key at `~/.minds-eval/aws.env`. See [SETUP.md](SETUP.md).

## Commands

```
# run an eval batch (one self-completing workspace per case) from a single config file
ANTHROPIC_API_KEY=sk-ant-... minds-evals launch --config eval-config.json

# status, straight from S3 -- no box, works any time from anywhere
minds-evals list-batches
minds-evals inspect combined_20260713-101500

# score a finished batch (S3 + Anthropic only, no box); writes results back to S3
ANTHROPIC_API_KEY=sk-ant-... minds-evals evaluate combined_20260713-101500

# see + open the live workspaces in the shared Modal env
minds-evals list-modal-workspaces
minds-evals view-modal-workspace EVAL-combined-CASE-todo-app   # prints a self-authenticating URL

# destroy a branch's Modal workspaces (clean slate)
minds-evals clean-modal-workspaces

# utilities
minds-evals box --mngr-branch minds-eval
minds-evals make-modal-workspace --mngr-branch minds-eval \
    --fct-link https://github.com/imbue-ai/default-workspace-template.git --fct-branch main
```

`launch`/`make-modal-workspace`/`clean-modal-workspaces` build/boot the box if needed and
re-run themselves inside it. `list-batches`/`inspect` only read S3 and run wherever you are. After a
box-using command, the dashboard + one-time workspace-login URL are printed (open the login URL once
per box, then click the workspace in the dashboard).

## Eval config (`--config`)

A single json, stored verbatim in S3 as the batch config (plus `created_at`, `restic_password`,
`mngr_sha`). See `eval-config.json`:

```json
{
  "name": "combined",
  "mngr_branch": "minds-eval",
  "fct_branch": "minds-eval-autosend",
  "personas": [
    {"id": "todo-app", "persona": "...", "prompts": ["Build me ...", "Sounds good.", "DECIDE_FROM_PERSONA"]}
  ]
}
```

Each case's `prompts` array is the conversation, one entry per turn -- so different cases can run
different numbers of turns. Each entry is either:

- a **literal string** -- sent to the agent verbatim (e.g. the opening ask, or `"Sounds good."`); or
- **`DECIDE_FROM_PERSONA`** -- the in-sandbox worker role-plays the client: it feeds the
  transcript-so-far + the case's `persona` to the Anthropic API (using the key `launch` was given)
  and sends back a short casual reply. It cannot be the first entry (nothing to decide from yet).

`fct_branch`/`fct_repo` are optional (default the workspace-template branch that carries the eval
worker). `fct_branch` must carry the worker or the sandbox boots but never self-runs.

## Turn logic (N = `len(prompts)`)

| turn | the in-sandbox worker does |
|---|---|
| 1 | send `prompts[0]` (a literal -- the opening ask) |
| 2 .. N | `restic backup /mngr --tag post_message_<k>`, then send `prompts[k]` (literal, or a role-played reply for `DECIDE_FROM_PERSONA`) |
| after N | upload the full transcript, mark `finished`, exit |

Each turn writes `state.json` (`waits_done` / `num_turns` / `ongoing`|`finished`). Snapshots are
captured to S3 per turn; spinning one back up as a live workspace (restore) is not implemented yet.

## Evaluating a finished batch (`evaluate`)

`minds-evals evaluate <batch>` reads the batch from S3 (no box, no Modal), nukes any prior results,
then scores every **finished** case in parallel and writes results back. Cases that aren't finished
yet (or whose eval errors) show as `N/A` rows and are left out of the batch average -- so a batch
with a straggler can still be scored for the rest. The per-case outputs are:

- `avg_word_count` -- average words per agent turn, from the transcript.
- `conciseness_score` / `nontechnical_language_score` / `proactive_score` -- three 1-10 scores from
  one Anthropic call per case (needs `ANTHROPIC_API_KEY`).

Per-case results land in `<case>/case_eval_results.json`, the batch average in
`<batch>/batch_eval_results.json`, and a table (rows = cases, columns = the keys, plus a batch-average
row) is printed. Add a new evaluation by appending a function to `EVALUATIONS` in `evaluate.py`.

## Viewing workspaces (`list-modal-workspaces` / `view-modal-workspace`)

All eval workspaces live in one shared Modal env and share one SSH key, so any running box can reach
any of them. But the box's built-in `mngr forward` eagerly proxies the *whole* env (a per-agent
Python stream + SSH tunnel each), which OOMs past ~20 live workspaces. So viewing is decoupled:

- `list-modal-workspaces` -- every workspace in the env (name + agent id) and each running box's memory.
- `view-modal-workspace <name>` -- runs a **scoped** `mngr forward` for that one workspace on a box's
  forward port and prints a self-authenticating `http://localhost:<port>/login?...` URL. One workspace
  forwarded -> cheap, so it works no matter how many workspaces exist. Picks the least-loaded running
  box by default; `--box <container>` / `--new-box-on-mngr-branch <branch>` / `--service <name>` override.

Keep the env lean anyway (`clean-modal-workspaces` after `evaluate` pulls results to S3) -- the box's
own eager forward still tries to proxy everything and will OOM a box if the env grows large.

## S3 layout

```
<name>_<utc-datetime>/               batch
  config.json                        the eval config verbatim + created_at + restic_password + mngr_sha
  <name>_<case_id>/
    state.json                       written by the worker each turn
    artifacts/full_transcript.jsonl  written by the worker on the final turn
    restic/                          the case's restic repo (tagged /mngr snapshots)
```

## Structure

```
main.py            argparse dispatch; re-invokes itself inside the box for box-using commands
box.py             Docker box lifecycle (build/boot minds-box-<branch>-<sha>, idempotent reuse)
minds_client.py    the Minds create API (POST + poll) -- shared by launch/workspace
launch.py          batch: prep clone (+ vendor mngr + slot test_case_metadata.json) and create per case
workspace.py       create one Modal workspace (build_payload + create_workspace) -- the one create path
status.py          list-batches / inspect / case_report (S3 reads only)
view.py            list-modal-workspaces / view-modal-workspace (scoped `mngr forward` per workspace)
evaluate.py        evaluate: pull transcripts, score (avg_word_count + LLM scores), write results to S3
anthropic_call.py  one plain Anthropic Messages call (the LLM-graded evals)
s3_store.py        S3 layout, creds file, batch/case prefixes
docker/            Dockerfile + entrypoint.sh (boots headless Minds in the box)
```

## Notes

- Snapshots use restic (deduped, encrypted, incremental). The worker drives restic itself with
  credentials + repo + password slotted into each clone's `scripts/test_case_metadata.json`
  (backup_provider is `configure_later`), because minds' `api_key` backup provider does not reliably
  land a `restic.env` inside a Modal sandbox.
- The eval worker lives on the FCT `minds-eval-autosend` branch and **no-ops unless
  `scripts/test_case_metadata.json` is present**, so normal workspaces on that branch are unaffected.
- Deps (`node_modules`, `.venv`) are excluded from snapshots; a future restore reinstalls them from
  the preserved lockfiles (they're captured but there is no restore command yet).
- The sandbox timeout is 3h (set in the FCT template); a run self-terminates by then.
