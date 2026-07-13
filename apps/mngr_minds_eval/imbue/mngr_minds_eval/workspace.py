"""Create ONE workspace in a running box. The single place that knows the create payload shape;
used both as the `workspace` utility (ad-hoc) and per-case by `launch`.

fct_link / fct_branch pass through verbatim (a git URL, a local /work/clones/<x> path, empty
branch, etc.)."""

from __future__ import annotations

from imbue.mngr_minds_eval import minds_client


def build_payload(
    *, fct_link: str, fct_branch: str, name: str, compute: str, ai_provider: str,
    anthropic_key: str, backup_provider: str,
) -> dict:
    """Create-form fields. Empty branch: a local clone is already on its commit, and passing a
    branch trips mngr's checkout_branch(FETCH_HEAD) on the use-in-place path."""
    payload = {
        "git_url": fct_link, "branch": fct_branch,
        "launch_mode": compute.upper(), "ai_provider": ai_provider.upper(),
        "backup_provider": backup_provider.upper(),
    }
    if anthropic_key:  # omit for SUBSCRIPTION (e.g. restore); API_KEY provider needs it
        payload["anthropic_api_key"] = anthropic_key
    if name:
        payload["host_name"] = name
    return payload


def create_workspace(
    *, port: str, fct_link: str, fct_branch: str = "", name: str = "",
    compute: str = "modal", ai_provider: str = "api_key", anthropic_key: str = "",
    backup_provider: str = "configure_later", quiet: bool = False,
) -> str:
    """POST a create and wait; return the new agent id. Raises minds_client.CreateError on failure
    (callers decide whether to abort or continue). quiet suppresses the stage/up prints (launch
    prints its own per-case lines)."""
    payload = build_payload(
        fct_link=fct_link, fct_branch=fct_branch, name=name, compute=compute,
        ai_provider=ai_provider, anthropic_key=anthropic_key, backup_provider=backup_provider,
    )
    if not quiet:
        print(">> creating workspace {} from {}@{} ({}/{}) ...".format(
            name or "<auto>", fct_link, fct_branch or "<default>", compute, ai_provider), flush=True)
    on_stage = None if quiet else (lambda s: print("   ... {}".format(s), flush=True))
    agent_id = minds_client.create_and_wait(port, payload, on_stage=on_stage)
    if not quiet:
        print("  workspace up: {} (agent {})".format(name or "<auto>", agent_id), flush=True)
    return agent_id
