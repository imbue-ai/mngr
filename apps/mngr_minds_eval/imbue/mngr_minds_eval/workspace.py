"""Create ONE workspace in a running box -- a general utility for ad-hoc testing (no eval, no S3).

Args slot straight into the create endpoint; fct_link / fct_branch pass through verbatim (a git URL,
a local /work/clones/<x> path, empty branch, etc.)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def _api(port: str) -> str:
    return "http://127.0.0.1:{}".format(port)


def create_workspace(
    *, port: str, fct_link: str, fct_branch: str, name: str,
    compute: str, ai_provider: str, anthropic_key: str, backup_provider: str,
) -> None:
    payload = {
        "git_url": fct_link, "branch": fct_branch,
        "launch_mode": compute.upper(), "ai_provider": ai_provider.upper(),
        "anthropic_api_key": anthropic_key, "backup_provider": backup_provider.upper(),
    }
    if name:
        payload["host_name"] = name

    print(">> creating workspace {} from {}@{} ({}/{}) ...".format(
        name or "<auto>", fct_link, fct_branch or "<default>", compute, ai_provider), flush=True)
    request = urllib.request.Request(
        "{}/api/v1/workspaces".format(_api(port)), data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit("create failed HTTP {}: {}".format(exc.code, exc.read().decode()[:400]))
    operation_id = body.get("operation_id")
    if not operation_id:
        raise SystemExit("create returned no operation_id: {}".format(body))

    deadline = time.time() + 1800.0
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                "{}/api/v1/workspaces/operations/create/{}".format(_api(port), operation_id), timeout=30
            ) as response:
                info = json.loads(response.read().decode())
        except (urllib.error.URLError, OSError):
            time.sleep(4)
            continue
        stage = info.get("status_text") or info.get("status") or ""
        if stage and stage != last:
            print("   ... {}".format(stage), flush=True)
            last = stage
        if info.get("is_done"):
            print("  workspace up: {} (agent {})".format(name or "<auto>", info.get("agent_id")), flush=True)
            return
        if info.get("error"):
            raise SystemExit("create failed: {}".format(info["error"]))
        time.sleep(4)
    raise SystemExit("timed out waiting for the workspace")
