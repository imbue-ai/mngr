"""Thin client for the box-local Minds create API. Shared by launch / workspace so the
POST-then-poll workspace-creation logic lives in exactly one place."""

from __future__ import annotations

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


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode())


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
        if info.get("is_done"):
            agent_id = info.get("agent_id")
            if not isinstance(agent_id, str):
                raise CreateError("create finished without an agent_id: {}".format(info))
            return agent_id
        if info.get("error"):
            raise CreateError(str(info["error"]))
        time.sleep(4)
    raise CreateError("timed out waiting for workspace create")


def restart_and_wait(
    port: str, agent_id: str, *, timeout: float = 1800.0, on_stage: Callable[[str], None] | None = None
) -> None:
    """Bounce a workspace's host (restart the Modal sandbox) and poll until done. Streams each new
    status caption via on_stage. Raises CreateError on failure/timeout. Used to bring a stopped
    workspace back up before forwarding it."""
    status, body = post_json(
        "{}/api/v1/workspaces/{}/restart".format(api_base(port), agent_id),
        {"scope": "host", "host_already_stopped": True},
    )
    if status != 202:
        raise CreateError("restart failed HTTP {}: {}".format(status, body))
    operation_id = body.get("operation_id")
    if not operation_id:
        raise CreateError("restart returned no operation_id: {}".format(body))

    deadline = time.time() + timeout
    last_stage = ""
    while time.time() < deadline:
        try:
            info = get_json("{}/api/v1/workspaces/operations/restart/{}".format(api_base(port), operation_id))
        except (urllib.error.URLError, OSError):
            time.sleep(4)
            continue
        stage = info.get("status_text") or info.get("status") or ""
        if on_stage and stage and stage != last_stage:
            on_stage(stage)
            last_stage = stage
        state = (info.get("status") or "").upper()
        if state == "DONE" or info.get("is_done"):
            return
        if state == "FAILED" or info.get("error"):
            raise CreateError("restart failed: {}".format(info.get("error") or info))
        time.sleep(4)
    raise CreateError("timed out waiting for workspace restart")
