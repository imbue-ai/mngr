"""list-modal-workspaces / view-modal-workspace: see and open the workspaces in the shared Modal env.

`view-modal-workspace` runs a *scoped* `mngr forward` (a single workspace) inside a box, so it stays
cheap no matter how many workspaces the env holds -- unlike the box's built-in forward, which eagerly
proxies every workspace and OOMs past ~20. All boxes share one Modal SSH key, so any box can reach
any workspace; by default we pick the least-loaded running box.
"""

from __future__ import annotations

import json
import re

from imbue.mngr_minds_eval import box as box_mod
from imbue.mngr_minds_eval import minds_client

_MEM_RE = re.compile(r"\s*([0-9.]+)\s*([KMG]i?B)")
_MEM_UNITS = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0}


def _running_boxes() -> list[str]:
    out = box_mod._run(["docker", "ps", "--filter", "name=minds-box-", "--format", "{{.Names}}"]).stdout
    return [line.strip() for line in out.splitlines() if line.startswith("minds-box-")]


def _box_mem_mib(container: str) -> float:
    """Current memory usage of a box in MiB -- used to pick the least-loaded box to forward from."""
    out = box_mod._run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container]).stdout
    match = _MEM_RE.match(out)
    if not match:
        return 0.0
    return float(match.group(1)) * _MEM_UNITS.get(match.group(2), 1.0)


def _workspaces(container: str) -> list[dict]:
    """The workspaces the box's Minds sees in the shared env (name + agent_id)."""
    port = box_mod.port_of(container)
    out = box_mod._run(
        ["docker", "exec", container, "curl", "-sS", "-m", "5", "http://127.0.0.1:{}/api/v1/workspaces".format(port)]
    ).stdout
    try:
        return [w for w in json.loads(out).get("workspaces", []) if w.get("agent_id")]
    except (ValueError, TypeError):
        return []


def list_modal_workspaces() -> None:
    boxes = _running_boxes()
    if not boxes:
        raise SystemExit("no running box -- start one: minds-evals box --mngr-branch <branch>")
    workspaces = _workspaces(boxes[0])
    print("{} workspace(s) in the shared Modal env:".format(len(workspaces)))
    print("{:<40} {}".format("NAME", "AGENT"))
    for w in sorted(workspaces, key=lambda w: w.get("name") or ""):
        print("{:<40} {}".format((w.get("name") or "?")[:40], w.get("agent_id") or "?"))
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
    # least-loaded, so we don't pile onto an OOM-heavy box
    return min(boxes, key=_box_mem_mib)


def _mngr_ssh_endpoint(container: str, agent_id: str) -> tuple[str, str, int] | None:
    """(user, host, port) of the workspace's Modal sandbox, from `mngr list --format json` inside the
    box, matched by agent id (unambiguous). None if not found or unparseable."""
    out = box_mod._run(
        ["docker", "exec", container, "sh", "-lc", "cd /work/mngr && uv run mngr list --format json"]
    ).stdout
    try:
        parsed = json.loads(out)
    except (ValueError, TypeError):
        return None
    entries = parsed if isinstance(parsed, list) else (parsed.get("agents") or parsed.get("workspaces") or [])
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("id") != agent_id:
            continue
        ssh = (entry.get("host") or {}).get("ssh") or {}
        host, port = ssh.get("host"), ssh.get("port")
        if host and port:
            return str(ssh.get("user") or "root"), str(host), int(port)
    return None


def view_modal_workspace(name: str, *, box: str = "", new_box_on_mngr_branch: str = "", restart: bool = True) -> None:
    container = _pick_box(box, new_box_on_mngr_branch)
    match = next((w for w in _workspaces(container) if (w.get("name") or "") == name), None)
    if match is None:
        raise SystemExit("no workspace named {!r} in the env (see: minds-evals list-modal-workspaces)".format(name))
    agent_id = match["agent_id"]

    # A stopped/paused sandbox has no reachable sshd -- bring it back up first (with live progress).
    host_state = (match.get("host_state") or "").upper()
    if host_state == "DESTROYED":
        raise SystemExit("workspace {} is DESTROYED -- relaunch it (can't restart a destroyed sandbox)".format(name))
    if restart and host_state in ("STOPPED", "PAUSED"):
        print(">> {} is {}; restarting its sandbox ...".format(name, host_state), flush=True)
        try:
            minds_client.restart_and_wait(
                box_mod.port_of(container), agent_id, on_stage=lambda s: print("   ... {}".format(s), flush=True)
            )
        except minds_client.CreateError as exc:
            raise SystemExit("restart failed: {}".format(exc)) from exc

    endpoint = _mngr_ssh_endpoint(container, agent_id)
    if endpoint is None:
        raise SystemExit("could not resolve the SSH endpoint for {} (via mngr list) -- is it running?".format(name))
    user, ssh_host, ssh_port = endpoint

    # View = a plain host-side SSH local-forward to the workspace UI (a fixed :8000 in every sandbox),
    # authenticated with the shared key already on the host. No mngr forward, no OOM, O(1) per
    # workspace, branch-agnostic. `ssh -f` backgrounds the tunnel once it is up.
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
    print("\n  {} is viewable at:  http://localhost:{}/".format(name, local_port), flush=True)
    print("  (background SSH tunnel to the sandbox; kill the ssh process to close it)", flush=True)
