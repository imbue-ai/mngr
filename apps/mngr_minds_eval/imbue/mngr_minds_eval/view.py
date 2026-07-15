"""list-modal-workspaces / view-modal-workspace: see and open the workspaces in the shared Modal env.

Both read the box's Minds API -- the same discovery the dashboard uses, and the source of truth for
which workspaces exist and their SSH endpoints. `view-modal-workspace` then opens a plain host-side
`ssh -L` straight to the workspace UI (a fixed :8000 in every sandbox) using the one shared Modal key,
so it is O(1) per workspace (never scales with env size, never OOMs) and branch-agnostic. Every box
mounts that key, so any box (and the host) can reach any workspace; by default we pick the
least-loaded running box to read from.
"""

from __future__ import annotations

import re

from imbue.mngr_minds_eval import box as box_mod
from imbue.mngr_minds_eval import minds_client

_MEM_RE = re.compile(r"\s*([0-9.]+)\s*([KMG]i?B)")
_MEM_UNITS = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0}
# Tag for the temporary authorized_keys grant the Minds SSH endpoint records for us.
_SSH_REQUESTER = "minds-evals-viewer"


def _running_boxes() -> list[str]:
    out = box_mod._run(["docker", "ps", "--filter", "name=minds-box-", "--format", "{{.Names}}"]).stdout
    return [line.strip() for line in out.splitlines() if line.startswith("minds-box-")]


def _box_mem_mib(container: str) -> float:
    """Current memory usage of a box in MiB -- used to pick the least-loaded box to read from."""
    out = box_mod._run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container]).stdout
    match = _MEM_RE.match(out)
    if not match:
        return 0.0
    return float(match.group(1)) * _MEM_UNITS.get(match.group(2), 1.0)


def _workspaces(container: str) -> list[dict]:
    """Workspaces the box's Minds has discovered in the shared env (name, agent_id, host_state)."""
    return minds_client.list_workspaces(box_mod.port_of(container))


def list_modal_workspaces() -> None:
    boxes = _running_boxes()
    if not boxes:
        raise SystemExit("no running box -- start one: minds-evals box --mngr-branch <branch>")
    try:
        workspaces = _workspaces(boxes[0])
    except minds_client.CreateError as exc:
        raise SystemExit(str(exc)) from exc
    print("{} workspace(s) in the shared Modal env:".format(len(workspaces)))
    print("{:<40} {:<10} {}".format("NAME", "STATE", "AGENT"))
    for w in sorted(workspaces, key=lambda w: w.get("name") or ""):
        print(
            "{:<40} {:<10} {}".format(
                (w.get("name") or "?")[:40], (w.get("host_state") or "?")[:10], w.get("agent_id") or "?"
            )
        )
    print("\nrunning boxes (memory -- lowest is picked by default for viewing):")
    for container in sorted(boxes, key=_box_mem_mib):
        print("  {:<36} {:>6.0f} MiB".format(container, _box_mem_mib(container)))
    print("\nview one:  minds-evals view-modal-workspace <NAME>")


def _pick_box(box: str, new_box_on_mngr_branch: str) -> str:
    if box:
        if not box_mod.is_running(box):
            raise SystemExit("box {} is not running".format(box))
        return box
    if new_box_on_mngr_branch:
        return box_mod.ensure(new_box_on_mngr_branch)
    boxes = _running_boxes()
    if not boxes:
        raise SystemExit("no running box -- pass --new-box-on-mngr-branch <branch> to start one")
    # least-loaded, so we don't pile onto a memory-heavy box
    return min(boxes, key=_box_mem_mib)


def _shared_public_key() -> str:
    """The public half of the shared Modal key (already in every eval workspace's authorized_keys).
    We hand it to the Minds SSH endpoint, which refreshes the grant and returns the sandbox address."""
    key = box_mod.SHARED_MODAL_KEYS / "modal_ssh_key"
    result = box_mod._run(["ssh-keygen", "-y", "-f", str(key)])
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit("could not read the shared SSH public key from {}".format(key))
    return result.stdout.strip()


def view_modal_workspace(name: str, *, box: str = "", new_box_on_mngr_branch: str = "", restart: bool = True) -> None:
    container = _pick_box(box, new_box_on_mngr_branch)
    port = box_mod.port_of(container)
    try:
        match = next((w for w in _workspaces(container) if (w.get("name") or "") == name), None)
    except minds_client.CreateError as exc:
        raise SystemExit(str(exc)) from exc
    if match is None:
        raise SystemExit("no workspace named {!r} in the env (see: minds-evals list-modal-workspaces)".format(name))
    agent_id = match.get("agent_id")
    if not agent_id:
        raise SystemExit("workspace {!r} has no agent id yet (still resolving) -- try again shortly".format(name))

    # A stopped/paused sandbox has no reachable sshd -- bring it back up first (with live progress).
    host_state = (match.get("host_state") or "").upper()
    if host_state == "DESTROYED":
        raise SystemExit("workspace {} is DESTROYED -- relaunch it (can't restart a destroyed sandbox)".format(name))
    if restart and host_state in ("STOPPED", "PAUSED"):
        print(">> {} is {}; restarting its sandbox ...".format(name, host_state), flush=True)
        try:
            minds_client.restart_and_wait(port, agent_id, on_stage=lambda s: print("   ... {}".format(s), flush=True))
        except minds_client.CreateError as exc:
            raise SystemExit("restart failed: {}".format(exc)) from exc

    # Ask Minds for the sandbox's SSH endpoint (it authorizes our shared key + returns the address).
    try:
        user, ssh_host, ssh_port = minds_client.establish_ssh(port, agent_id, _shared_public_key(), _SSH_REQUESTER)
    except minds_client.CreateError as exc:
        raise SystemExit("could not resolve the SSH endpoint for {}: {}".format(name, exc)) from exc

    # View = a plain host-side SSH local-forward to the workspace UI (a fixed :8000 in every sandbox),
    # authenticated with the shared key already on the host. `ssh -f` backgrounds the tunnel once up.
    local_port = box_mod._free_port()
    key = box_mod.SHARED_MODAL_KEYS / "modal_ssh_key"
    known_hosts = box_mod.SHARED_MODAL_KEYS / "known_hosts"
    result = box_mod._run(
        [
            "ssh",
            "-N",
            "-f",
            "-L",
            "{}:127.0.0.1:8000".format(local_port),
            "-p",
            str(ssh_port),
            "-i",
            str(key),
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile={}".format(known_hosts),
            "-o",
            "ExitOnForwardFailure=yes",
            "{}@{}".format(user, ssh_host),
        ]
    )
    if result.returncode != 0:
        raise SystemExit("ssh tunnel to {} failed: {}".format(name, (result.stderr or "").strip()[:300]))

    # The tunnel can open before the workspace's UI (:8000) is actually serving -- a freshly-created
    # sandbox is still booting its services. Probe once so we can tell the user the truth instead of
    # handing over a URL that shows "connection reset". The tunnel stays valid either way, so once the
    # UI comes up a plain reload works.
    probe = box_mod._run(
        ["curl", "-s", "-m", "6", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:{}/".format(local_port)]
    )
    serving = probe.returncode == 0 and probe.stdout.strip() not in ("", "000")
    print("\n  {} is viewable at:  http://localhost:{}/".format(name, local_port), flush=True)
    if serving:
        print("  (background SSH tunnel to the sandbox; kill the ssh process to close it)", flush=True)
    else:
        print("  NOTE: its UI isn't serving yet -- the sandbox is still booting, so the page will show", flush=True)
        print("  'connection reset' for a minute or two. Just reload once it's up; the tunnel stays open.", flush=True)
