"""Create ONE workspace in a running box -- a general utility for ad-hoc testing (no eval, no S3).

Args slot straight into the create endpoint; fct_link / fct_branch pass through verbatim (a git URL,
a local /work/clones/<x> path, empty branch, etc.)."""

from __future__ import annotations

from imbue.mngr_minds_eval import minds_client


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
    try:
        agent_id = minds_client.create_and_wait(port, payload, on_stage=lambda s: print("   ... {}".format(s), flush=True))
    except minds_client.CreateError as exc:
        raise SystemExit(str(exc))
    print("  workspace up: {} (agent {})".format(name or "<auto>", agent_id), flush=True)
