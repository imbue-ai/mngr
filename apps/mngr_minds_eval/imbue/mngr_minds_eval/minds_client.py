"""Thin client for the box-local Minds create API. Shared by launch / workspace so the
POST-then-poll workspace-creation logic lives in exactly one place."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable


class CreateError(RuntimeError):
    pass


def api_base(port: str) -> str:
    return "http://127.0.0.1:{}".format(port)


def post_json(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:400]}
    except (urllib.error.URLError, OSError) as exc:
        # Connection refused/dropped (box not up yet, transient blip). Report as a non-2xx so the
        # caller raises CreateError instead of letting a raw traceback abort a whole batch.
        return 0, {"error": str(exc)}


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


def _listening_ports() -> list[int]:
    """Ports in LISTEN state on this machine's loopback/any interfaces, from /proc/net/tcp{,6}
    (Linux only -- we only ever call this inside the box)."""
    ports: set[int] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(table).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            # fields[1] = local "ADDR:PORT" in hex; fields[3] = state (0A = LISTEN)
            if len(fields) > 3 and fields[3] == "0A":
                try:
                    ports.add(int(fields[1].rsplit(":", 1)[1], 16))
                except (ValueError, IndexError):
                    continue
    return sorted(ports)


def discover_api_port(timeout: float = 300.0) -> str:
    """Find the Minds backend's API port from INSIDE the box. The desktop app's backend picks a
    random free port at boot, so we probe every listening port for /api/v1/workspaces until one
    answers like Minds. Retries until `timeout` -- the Electron app may still be booting when the
    launch CLI is exec'd in."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in _listening_ports():
            try:
                with urllib.request.urlopen("{}/api/v1/workspaces".format(api_base(str(port))), timeout=2) as resp:
                    data = json.loads(resp.read().decode())
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
                # Not HTTP (x11vnc/websockify), not Minds, or not up yet -- keep probing.
                continue
            if isinstance(data, dict) and "workspaces" in data:
                return str(port)
        time.sleep(3)
    raise CreateError("could not find the Minds API inside the box (is the Minds app still booting?)")


def create_and_wait(
    port: str, payload: dict, *, timeout: float = 1800.0, on_stage: Callable[[str], None] | None = None
) -> str:
    """POST a create request and poll until done; return the new agent id. Raises CreateError on any
    failure (bad status, operation error, timeout). on_stage is called with each new status caption."""
    status, body = post_json("{}/api/v1/workspaces".format(api_base(port)), payload)
    if status != 202:
        raise CreateError("create failed HTTP {}: {}".format(status, body))
    operation_id = body.get("operation_id")
    if not operation_id:
        raise CreateError("create returned no operation_id: {}".format(body))

    deadline = time.time() + timeout
    last_stage = ""
    while time.time() < deadline:
        try:
            info = get_json("{}/api/v1/workspaces/operations/create/{}".format(api_base(port), operation_id))
        except (urllib.error.URLError, OSError):
            time.sleep(4)
            continue
        stage = info.get("status_text") or info.get("status") or ""
        if on_stage and stage and stage != last_stage:
            on_stage(stage)
            last_stage = stage
        # minds only surfaces the agent_id in the operation status once the whole create is done (its
        # readiness probe included), so we wait for is_done -- there is no earlier "created" signal here.
        if info.get("is_done"):
            agent_id = info.get("agent_id")
            if not isinstance(agent_id, str):
                raise CreateError("create finished without an agent_id: {}".format(info))
            return agent_id
        if info.get("error"):
            raise CreateError(str(info["error"]))
        time.sleep(4)
    raise CreateError("timed out waiting for workspace create")
