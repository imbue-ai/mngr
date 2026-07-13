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

## Setup

One-time: an S3 bucket and a bucket-scoped IAM key at `~/.minds-eval/aws.env`. See [SETUP.md](SETUP.md).

## Commands

```
# run an eval batch (one self-completing workspace per case) from a single config file
ANTHROPIC_API_KEY=sk-ant-... minds-evals launch --config sample-eval-config.json

# status, straight from S3 -- no box, works any time from anywhere
minds-evals list-batches
minds-evals inspect web1_20260713-101500

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
`mngr_sha`). See `sample-eval-config.json`:

```json
{
  "name": "web1",
  "turns": 4,
  "mngr_branch": "minds-eval",
  "fct_branch": "minds-eval-autosend",
  "personas": [{"id": "todo-app", "persona": "...", "first_prompt": "Build me ..."}]
}
```

`fct_branch`/`fct_repo` are optional (default the workspace-template branch that carries the eval
worker). `fct_branch` must carry the worker or the sandbox boots but never self-runs.

## Turn logic (`turns: N`)

| wait | the in-sandbox worker does |
|---|---|
| 1 | send the case's `first_prompt` |
| 2 .. N-1 | `restic backup /mngr --tag post_message_<k>`, then send `OKAY` |
| N | upload the full transcript, mark `finished`, exit |

Each wait writes `state.json` (`waits_done` / `num_turns` / `ongoing`|`finished`). Snapshots are
captured to S3 per turn; spinning one back up as a live workspace (restore) is not implemented yet.

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
status.py          list-batches / inspect (S3 reads only)
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
