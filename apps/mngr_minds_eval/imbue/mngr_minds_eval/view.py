"""list-modal-workspaces / view-modal-workspace: see and open the workspaces in the shared Modal env.

`view-modal-workspace` runs a *scoped* `mngr forward` (a single workspace) inside a box, so it stays
cheap no matter how many workspaces the env holds -- unlike the box's built-in forward, which eagerly
proxies every workspace and OOMs past ~20. All boxes share one Modal SSH key, so any box can reach
any workspace; by default we pick the least-loaded running box.
"""

from __future__ import annotations

import json
import re
import time

from imbue.mngr_minds_eval import box as box_mod
from imbue.mngr_minds_eval import minds_client

_LOGIN_RE = re.compile(r"https?://\S*?/login\?one_time_code=[A-Za-z0-9_-]+")
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


def view_modal_workspace(
    name: str,
    *,
    box: str = "",
    new_box_on_mngr_branch: str = "",
    service: str = "system_interface",
    restart: bool = True,
) -> None:
    container = _pick_box(box, new_box_on_mngr_branch)
    match = next((w for w in _workspaces(container) if (w.get("name") or "") == name), None)
    if match is None:
        raise SystemExit("no workspace named {!r} in the env (see: minds-evals list-modal-workspaces)".format(name))
    agent_id = match["agent_id"]
    forward_port = box_mod.forward_port_of(container)
    log_path = "/tmp/view_{}.log".format(agent_id)

    # A stopped/paused sandbox can't be forwarded -- bring it back up first (with live progress).
    host_state = (match.get("host_state") or "").upper()
    if host_state == "DESTROYED":
        raise SystemExit(
            "workspace {} is DESTROYED -- relaunch it (a destroyed sandbox can't be restarted)".format(name)
        )
    if restart and host_state in ("STOPPED", "PAUSED"):
        print(">> {} is {}; restarting its sandbox ...".format(name, host_state), flush=True)
        try:
            minds_client.restart_and_wait(
                box_mod.port_of(container), agent_id, on_stage=lambda s: print("   ... {}".format(s), flush=True)
            )
        except minds_client.CreateError as exc:
            raise SystemExit("restart failed: {}".format(exc)) from exc
        print(">> {} is back up".format(name), flush=True)

    # Free the forward port: stop the box's built-in eager forward (the one that OOMs) so our scoped
    # forward -- which proxies just this one workspace -- can bind the already-published port.
    box_mod._run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-lc",
            "ps -eo pid,args | grep 'mngr forward' | grep -v latchkey | grep -v grep | awk '{print $1}' | xargs -r kill",
        ]
    )
    time.sleep(1)

    cel = 'agent.labels.workspace_display_name == "{}"'.format(name)
    launch = (
        "cd /work/mngr && MNGR__PROVIDERS__MODAL__USER_ID={} uv run mngr forward --service {} "
        "--agent-include '{}' --host 0.0.0.0 --port {} > {} 2>&1"
    ).format(box_mod.MODAL_ENV_USER_ID, service, cel, forward_port, log_path)
    box_mod._run(["docker", "exec", "-d", container, "sh", "-lc", launch])
    print(">> forwarding {} (service {}) via box {} ...".format(name, service, container), flush=True)

    login = ""
    for _ in range(20):
        time.sleep(2)
        log = box_mod._run(["docker", "exec", container, "sh", "-lc", "cat {} 2>/dev/null".format(log_path)]).stdout
        found = _LOGIN_RE.findall(log)
        if found:
            login = found[-1]
            break
    if login:
        print("\n  open this (authenticates + lands on the workspace):\n    {}".format(login), flush=True)
    else:
        print(
            "\n  forward started but no login URL yet -- check: docker exec {} cat {}".format(container, log_path),
            flush=True,
        )
        print("  then open: http://localhost:{}/".format(forward_port), flush=True)
