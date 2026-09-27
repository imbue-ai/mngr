"""Modal-workspace selection derived from a tier's committed deploy config.

Lives in minds (not the operator-only admin CLI) because the public
deployment-test helpers also need to derive the right Modal workspace for a
tier -- ``imbue.minds.deployment_tests.helpers`` builds subprocess envs that
pin ``MODAL_PROFILE`` exactly like a deploy-mode env activation does.
"""

from typing import Final

from loguru import logger

from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import load_deploy_config

# Env var the activation exports set to pin every ``modal`` CLI
# shellout to a specific workspace, regardless of which profile is
# marked ``active = true`` in ``~/.modal.toml``. Only exported by
# ``minds-admin env activate --deploy``; plain ``minds-admin env activate``
# emits ``unset MODAL_PROFILE`` so a previously-deploy-activated shell
# reverts cleanly.
MODAL_PROFILE_ENV_VAR: Final[str] = "MODAL_PROFILE"


def modal_profile_for_tier_or_none(tier: str) -> str | None:
    """Return the Modal profile name (``modal_workspace``) for ``tier``, or None.

    Reads ``apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`` and
    pulls the committed ``modal_workspace`` value. ``minds-admin env activate
    --deploy`` exports this as ``MODAL_PROFILE`` so every ``modal`` CLI
    shellout (deploy, secret create, environment create, etc.) is pinned to
    the right workspace regardless of what's marked ``active = true`` in
    ``~/.modal.toml``.

    Returns ``None`` when the tier has no deploy.toml on disk (e.g.
    a freshly-checked-out tree before tier config is committed) or
    the committed value is still the literal ``CHANGE_ME`` placeholder.
    Activation proceeds without ``MODAL_PROFILE`` in that case so the
    operator's existing ``modal token set`` setup still works.
    """
    try:
        deploy_config = load_deploy_config(tier)
    except EnvConfigError as exc:
        logger.warning(
            "Could not load deploy.toml for tier {!r} ({}); MODAL_PROFILE will not be exported. "
            "modal shellouts will fall back to ~/.modal.toml's active profile.",
            tier,
            exc,
        )
        return None
    workspace = str(deploy_config.modal_workspace)
    if not workspace or workspace == "CHANGE_ME":
        return None
    return workspace
