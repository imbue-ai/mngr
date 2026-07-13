# mngr-minds-eval

Eval harness for Minds. Launch a batch of persona cases as **self-completing** workspaces: each
sandbox drives its own conversation, snapshots `/mngr` to S3 after every turn, and uploads its
transcript at the end. Results are read back from S3 -- the launching machine does not need to
stay on, and there is nothing to poll.

## Setup

One-time: an S3 bucket and a bucket-scoped IAM key at `~/.minds-eval/aws.env`. See SETUP.md.

## Use

```
# launch a batch (one self-completing workspace per case)
ANTHROPIC_API_KEY=sk-ant-... minds-evals launch \
    --name web1 --personas sample-personas.json --turns 4 --mngr-branch main

# status, straight from S3 -- no box, works any time from anywhere
minds-evals list-batches
minds-evals inspect web1_20260713-101500

# bring a case's per-turn snapshot back up as a workspace and click through what the agent built
minds-evals restore web1_20260713-101500 --case todo-app --message 2
```

`launch` and `restore` need the box (Minds' create API, the clone dir, mngr), so they build/boot it
from `--mngr-branch` if it isn't already up and re-run themselves inside it -- one command, nothing
to spin up by hand. `list-batches` / `inspect` only read S3 and run wherever you are.

Minds always runs in the Docker box (that is the mngr-branch isolation); workspaces always run on
Modal, including restored ones.

## Turn logic (`--turns N`, from the case config)

| wait | the in-sandbox worker does |
|---|---|
| 1 | send the case's `first_prompt` |
| 2 .. N-1 | `restic backup /mngr --tag post_message_<k>`, then send `OKAY` |
| N | upload the full transcript, mark `finished`, exit |

Every wait writes `state.json` (`waits_done` / `num_turns` / `ongoing`|`finished`).

## S3 layout

```
<eval_name>_<utc-datetime>/          batch
  config.json                        cases + num_turns + restic password
  <eval_name>_<case_name>/
    state.json                       written by the worker each turn
    artifacts/full_transcript.jsonl  written by the worker on the final turn
    restic/                          the case's restic repo (tagged /mngr snapshots)
```

## Notes

- Snapshots are restic (deduped, encrypted, incremental), configured through the create form's
  `api_key` backup provider -- the idiomatic path. The worker stops the `host-backup` service in
  its own sandbox so restic only fires on the turns it chooses; `supervisord.conf` is unchanged, so
  non-eval workspaces are unaffected.
- The eval worker lives on the FCT `minds-eval-autosend` branch and no-ops unless
  `scripts/config.json` is present, so normal workspaces on that branch behave normally.
- Deps (`node_modules`, `.venv`) are excluded from snapshots; a restored workspace reinstalls them
  from the preserved lockfiles on first boot.
