# minds.app VM test harness

End-to-end testing for the packaged `minds.app` Electron bundle, running
inside fresh macOS VMs via [Tart](https://tart.run/). The harness installs
the supplied app bundle, launches the backend, creates an agent against
`forever-claude-template`, sends a deterministic message, and verifies the
agent reply appears in the structured events log.

This is integration test infrastructure, not pytest tests: you run it on
your own Mac, on demand, against any built `minds.app` artifact.

## One-time host setup

```bash
brew install cirruslabs/cli/tart
brew install hudochenkov/sshpass/sshpass

# Pull the pristine macOS base image (~24 GB, cached after the first pull).
tart pull ghcr.io/cirruslabs/macos-tahoe-vanilla:latest

# Build the persona image. From the repo root:
apps/minds/scripts/vm-testing/build-persona.sh minds-fresh
```

`build-persona.sh` clones the cirruslabs base, applies the per-persona
provisioning script (`personas/minds-fresh.sh` for v1: no-op-ish), shuts
the VM down, and registers it as a local Tart image (`tart list` will
show it). Subsequent test runs `tart clone` from this image, which takes
seconds.

## Credentials

The created agent runs `claude` and so needs Anthropic credentials. A
freshly-cloned VM has none. Easiest path: export `ANTHROPIC_API_KEY` in
your host shell before running the harness:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

The orchestrator forwards it into the VM and tells the agent-creation API
to use `ai_provider=API_KEY` with that value. Set `AI_PROVIDER=SUBSCRIPTION`
explicitly (and leave `ANTHROPIC_API_KEY` unset) to instead exercise the
SUBSCRIPTION path -- which will fail until the persona pre-provisions
`~/.claude/.credentials.json`.

## Running a test

Against a ToDesktop build URL:

```bash
apps/minds/scripts/vm-testing/run-test.sh \
    https://dl.todesktop.com/26032588hqdzk/builds/<id>/mac/zip/arm64 \
    minds-fresh
```

Against a local artifact (the script auto-detects `.dmg`, `.zip`, or an
already-unzipped `.app`):

```bash
apps/minds/scripts/vm-testing/run-test.sh ~/Downloads/minds.dmg minds-fresh
apps/minds/scripts/vm-testing/run-test.sh ~/Downloads/minds.app.zip minds-fresh
apps/minds/scripts/vm-testing/run-test.sh /Applications/minds.app minds-fresh
```

For debugging, pass `--keep-vm` and the throwaway VM is left running after
the harness exits so you can `tart ip` it and `ssh admin@<ip>` (password
`admin`) for an inspection session:

```bash
apps/minds/scripts/vm-testing/run-test.sh <build> minds-fresh --keep-vm
# later:
tart stop minds-fresh-run-<ts>
tart delete minds-fresh-run-<ts>
```

Results land in `apps/minds/scripts/vm-testing/.results/<ts>-<persona>/`
(gitignored):

- `junit.xml` -- one `<testcase>` per harness step (install, launch,
  wait_for_backend, create_agent, send_message), with `<failure>` on the
  first failed step.
- `summary.json` -- machine-readable: per-step pass/fail, timings, the
  resolved `agent_id`, and the error from any failed step.
- `minds.log`, `minds-events.jsonl`, `launcher.log` -- raw artifacts copied
  out of the VM for post-mortem.
- `harness-stdout.log` -- streamed stdout/stderr from the in-VM harness.

The script exits 0 on pass, nonzero on any failed step.

## What the harness verifies (v1)

One persona (`minds-fresh`), one happy path:

1. **wipe_minds_state** -- removes `~/.minds/` and any leftover
   `/Applications/minds.app` so each run starts identically.
2. **install_app** -- extracts the supplied bundle into `/Applications/`.
   In practice the orchestrator stages a tarball (virtiofs misreports the
   Electron framework symlinks as cyclic), so the harness `tar -xf`s it; a
   plain `.app` directory works too via `ditto` as a fallback.
3. **launch_app** -- exec's `/Applications/minds.app/Contents/MacOS/minds`
   directly with `SKIP_AUTH=1`. (`open /Applications/minds.app` would not
   propagate env vars because LaunchServices launches fresh.)
4. **wait_for_backend** -- parses the dynamic port from
   `~/.minds/logs/minds.log` (`Bare-origin: http://127.0.0.1:<port>`),
   falls back to `ps -axo command`, then polls `GET /` until 200 (default
   timeout 300 s -- accommodates the cold-cache `uv sync` of bundled
   deps).
5. **create_agent** -- `POST /api/create-agent` with the
   `forever-claude-template` repo as `git_url` and `launch_mode=LOCAL` (the
   default), then polls `GET /api/create-agent/{id}/status` until `DONE`
   or `FAILED` (default 600 s). The Cookie header is
   `minds_session=skip`; SKIP_AUTH=1 makes that valid.
6. **send_message** -- runs the bundled
   `uv run mngr message <host_name> -m <prompt>` via the resources shipped
   inside `minds.app/Contents/Resources/`, then tails
   `~/.minds/logs/minds-events.jsonl` for the expected response substring
   (default `PINGPONG-OK`; the prompt asks the agent to print it
   literally).

## Adding a new persona

Drop a `personas/<name>.sh` provisioning script (executable, idempotent)
and run `build-persona.sh <name>`. Provisioning runs as the `admin` user
over SSH, with no password prompts (sudoers is already configured by the
cirruslabs base for the `admin` user).

Forthcoming personas (separate work items, not in this PR):
- `minds-vanilla-brew` -- Homebrew installed, brew python on PATH.
- `minds-stale-path` -- a pre-existing, outdated `~/.minds/` from an
  older build to exercise upgrade.
- `minds-non-admin` -- the user is a Standard account, no sudo.

## Known gotchas

- **`tart run` and the VNC flag**: scripts always pass `--vnc-experimental`
  because without it the VM window does not attach to your active GUI
  session when `tart run` is invoked from a non-interactive shell.
- **`open` vs. direct exec**: `open /Applications/minds.app` does not
  propagate env vars (LaunchServices launches the app fresh). The harness
  exec's the binary inside `Contents/MacOS/` directly so `SKIP_AUTH=1`
  takes effect.
- **`mngr message` watchdog flake**: mngr's TUI submission watchdog can
  time out at 90 s with a failure return code even when the keystroke
  landed. The harness treats the exit code as advisory and confirms the
  send by tailing `minds-events.jsonl` for the expected response.
- **`screencapture` over SSH fails inside the VM**
  (`could not create image from display`). v1 omits screenshots; if you
  need one, attach to the VM via Screen Sharing on the host and use
  `screencapture` against the Screen Sharing window.
- **zsh vs. bash word splitting**: zsh does not word-split unquoted vars
  (`$SSHOPTS`). The scripts use bash arrays + `bash` shebangs to avoid the
  pitfall; if you copy snippets out of this dir into zsh, use `${=VAR}`.
- **Shared volume is read-only inside the VM**: writes from the guest
  side fail with `EROFS`. The harness writes results to `/tmp/minds-...`
  inside the VM and the orchestrator `scp`s them back out.
- **IP appears before sshd**: `tart ip --wait` returns as soon as DHCP
  hands out a lease, but sshd needs another ~30 s. The shared `lib.sh`
  helper polls SSH after the IP comes up.

## Future work

- Additional personas (`minds-vanilla-brew`, `minds-stale-path`,
  `minds-non-admin`).
- CI integration: wrap `run-test.sh` in a GitHub Actions workflow on a
  self-hosted macOS runner.
- Talk to the FCT workspace_server chat endpoint directly over HTTP
  instead of going through `mngr message` (removes the 90 s watchdog
  flake; needs a grep of FCT for the route).
- Drive the Electron shell UI (Playwright `_electron` or osascript) to
  cover behaviors that bypass the HTTP API.
- Opt-in quarantine xattr step to exercise Gatekeeper's first-launch
  flow (the harness already accepts `APPLY_QUARANTINE=1`).
