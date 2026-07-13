# mngr-minds-eval

`minds-evals` — a harness for running persona-based evals against Minds.

Launch a batch of persona cases as **self-completing** Modal workspaces: each sandbox drives its own
multi-turn conversation, snapshots `/mngr` to S3 after each turn (restic), and uploads its
transcript at the end. Results are read back from S3, so the launching machine does not need to
stay on and there is nothing to poll.

## How it fits together

- **The box** — a local Docker container running headless Minds. It is the mngr-branch isolation and
  the thing that talks to Modal + serves you the UI on localhost. Built from a branch's remote tip
  (rebuilt only when the branch moves).
- **Workspaces** — always Modal sandboxes. Never run in the box.
- **Eval flows** (`launch`/`restore`/`clean-modal-workspaces`) key the box **and** the Modal env on
  the **eval name**: a run's sandboxes live in `minds-staging-<name>` (box `minds-box-<name>`), so
  they're findable and separable. `--mngr-branch` is just which mngr goes *into* the box.
- **Utilities** (`box`/`workspace`) key on the **branch** — they aren't eval-scoped.

## Setup

One-time: an S3 bucket and a bucket-scoped IAM key at `~/.minds-eval/aws.env`. See [SETUP.md](SETUP.md).

## Commands

```
# run an eval batch (one self-completing workspace per case)
ANTHROPIC_API_KEY=sk-ant-... minds-evals launch \
    --name web1 --personas sample-personas.json --turns 4 \
    --mngr-branch minds-eval --fct-branch minds-eval-autosend

# status, straight from S3 -- no box, works any time from anywhere
minds-evals list-batches
minds-evals inspect web1_20260713-101500

# restore a case's per-turn snapshot into a fresh Modal workspace, seeded with its chat history
minds-evals restore web1_20260713-101500 --case todo-app --message 2

# destroy an eval's Modal workspaces (clean slate)
minds-evals clean-modal-workspaces --name web1

# utilities (branch-scoped, not eval-scoped)
minds-evals box --mngr-branch minds-eval
minds-evals workspace --mngr-branch minds-eval \
    --fct-link https://github.com/imbue-ai/default-workspace-template.git --fct-branch main
```

`launch`/`restore`/`workspace`/`clean-modal-workspaces` build/boot the box if needed and re-run
themselves inside it. `list-batches`/`inspect` only read S3 and run wherever you are. After a
box-using command, the dashboard + one-time workspace-login URL are printed (open the login URL once
per box, then click the workspace in the dashboard).

## `launch` flags

| flag | meaning | default |
|---|---|---|
| `--name` | eval name; batch folder `<name>_<utc>`, box + Modal env `<name>` | required |
| `--personas` | cases json: `[{id, persona, first_prompt}]` | required |
| `--turns` | waits the worker runs (>= 2) | 4 |
| `--mngr-branch` | mngr the box runs (and vendors into each case) | main |
| `--fct-branch` / `--fct-repo` | workspace template each case is cloned from (must carry the eval worker) | `minds-eval-autosend` |
| `--anthropic-key` | Anthropic key | `$ANTHROPIC_API_KEY` |

## Turn logic (`--turns N`)

| wait | the in-sandbox worker does |
|---|---|
| 1 | send the case's `first_prompt` |
| 2 .. N-1 | `restic backup /mngr --tag post_message_<k>`, then send `OKAY` |
| N | upload the full transcript, mark `finished`, exit |

Each wait writes `state.json` (`waits_done` / `num_turns` / `ongoing`|`finished`).
So `--turns 3` yields exactly one snapshot, `post_message_1` (restore with `--message 1`).

## S3 layout

```
<eval_name>_<utc-datetime>/          batch
  config.json                        cases + num_turns + mngr_branch + fct + restic password
  <eval_name>_<case_name>/
    state.json                       written by the worker each turn
    artifacts/full_transcript.jsonl  written by the worker on the final turn
    restic/                          the case's restic repo (tagged /mngr snapshots)
```

## Structure

```
main.py            argparse dispatch; re-invokes itself inside the box for box-using commands
box.py             Docker box lifecycle (build/boot, SHA-keyed reuse, login URL)
minds_client.py    the Minds create API (POST + poll) -- shared by launch/workspace/restore
launch.py          batch: prep clone (+ vendor mngr + slot config.json) and create per case
workspace.py       one ad-hoc workspace (utility)
restore.py         restic restore -> fresh Modal workspace -> seed /mngr agent state
status.py          list-batches / inspect (S3 reads only)
s3_store.py        S3 layout, creds file, batch/case prefixes
docker/            Dockerfile + entrypoint.sh (boots headless Minds in the box)
```

## Notes

- Snapshots use restic (deduped, encrypted, incremental). The worker drives restic itself with
  credentials + repo + password slotted into each clone's `config.json` (backup_provider is
  `configure_later`), because minds' `api_key` backup provider does not reliably land a `restic.env`
  inside a Modal sandbox.
- The eval worker lives on the FCT `minds-eval-autosend` branch and **no-ops unless
  `scripts/config.json` is present**, so normal workspaces on that branch are unaffected.
- Deps (`node_modules`, `.venv`) are excluded from snapshots; a restored workspace reinstalls them
  from the preserved lockfiles on first boot.
- The sandbox timeout is 3h (set in the FCT template); a run self-terminates by then.
