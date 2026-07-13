"""Create ONE workspace in a running box. The single place that knows the create payload shape;
used both as the `workspace` utility (ad-hoc) and per-case by `launch`.

fct_link / fct_branch pass through verbatim (a git URL, a local /work/clones/<x> path, empty
branch, etc.)."""

from __future__ import annotations

from imbue.mngr_minds_eval import minds_client


def build_payload(*, fct_link: str, fct_branch: str, name: str, ai_provider: str,
                  anthropic_key: str, backup_provider: str) -> dict:
    """Create-form fields. Workspaces are always Modal. Empty branch: a local clone is already on its
    commit, and passing a branch trips mngr's checkout_branch(FETCH_HEAD) on the use-in-place path."""
    payload = {
        "git_url": fct_link, "branch": fct_branch,
        "launch_mode": "MODAL", "ai_provider": ai_provider.upper(),
        "backup_provider": backup_provider.upper(),
    }
    if anthropic_key:  # omit for a SUBSCRIPTION workspace; API_KEY provider needs it
        payload["anthropic_api_key"] = anthropic_key
    if name:
        payload["host_name"] = name
    return payload


def create_workspace(
    *, port: str, fct_link: str, fct_branch: str = "", name: str = "",
    ai_provider: str = "api_key", anthropic_key: str = "",
    backup_provider: str = "configure_later", quiet: bool = False, on_stage=None,
) -> str:
    """POST a Modal create and wait; return the new agent id. Raises minds_client.CreateError on
    failure (callers decide whether to abort or continue). Pass on_stage(caption) to route progress
    (launch's live table does this); else quiet suppresses prints, or it prints its own lines."""
    payload = build_payload(
        fct_link=fct_link, fct_branch=fct_branch, name=name,
        ai_provider=ai_provider, anthropic_key=anthropic_key, backup_provider=backup_provider,
    )
    if on_stage is None and not quiet:
        print(">> creating modal workspace {} from {}@{} ({}) ...".format(
            name or "<auto>", fct_link, fct_branch or "<default>", ai_provider), flush=True)
        on_stage = lambda s: print("   ... {}".format(s), flush=True)
    agent_id = minds_client.create_and_wait(port, payload, on_stage=on_stage)
    if on_stage is None and not quiet:
        print("  workspace up: {} (agent {})".format(name or "<auto>", agent_id), flush=True)
    return agent_id
