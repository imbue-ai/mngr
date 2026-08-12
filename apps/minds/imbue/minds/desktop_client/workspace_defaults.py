"""Defaults for the workspace create flow: template repo URL and ref.

These are the create form's Repository / Version defaults, and the values a
plain ``mngr create`` from minds uses when the user does not override them.
Overridable via the MINDS_WORKSPACE_* env vars only when the operator
explicitly opts in -- see ``_operator_workspace_default`` for the gating
rationale.
"""

import os
from typing import Final

# Public alias: the default-workspace-template repo URL. The pre-baked Lima
# image gate (lima_image_prefetch) keys on this to recognize the default workspace.
DEFAULT_WORKSPACE_TEMPLATE_GIT_URL: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"
_FALLBACK_GIT_URL: Final[str] = DEFAULT_WORKSPACE_TEMPLATE_GIT_URL
# Pin to an annotated DEFAULT_WORKSPACE_TEMPLATE tag so a shipped binary clones the exact DEFAULT_WORKSPACE_TEMPLATE
# snapshot it was verified against. Bump to a newer tag only after
# re-verifying launch-to-msg CI against (this binary, the new tag).
FALLBACK_BRANCH: Final[str] = "minds-v0.3.11"

# Env var (set by ``just minds-start`` and the e2e workspace runner) that opts a
# launch into the operator's local-worktree create-form defaults. Gating on an
# explicit opt-in -- rather than on the tier -- means dev iteration works on ANY
# tier (including staging / production) when launched via ``just minds-start``,
# while a normal end-user ``minds run`` never honors a stray MINDS_WORKSPACE_*
# left over in the operator's shell, on any tier. The previous tier-based gate
# did the opposite: it blocked legitimate dev iteration on staging (forcing the
# form back to the public GitHub DEFAULT_WORKSPACE_TEMPLATE on ``main``) while leaving dev tiers exposed
# to stray vars.
_WORKSPACE_DEFAULTS_OPT_IN_ENV_VAR: Final[str] = "MINDS_USE_LOCAL_WORKSPACE_DEFAULTS"


def is_local_workspace_defaults_opt_in() -> bool:
    """Return whether the operator opted into local-worktree create-form defaults (the dev loop).

    True when ``MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1`` -- the same signal that
    routes the create form at the operator's local DEFAULT_WORKSPACE_TEMPLATE worktree. The pre-baked
    image gate treats this as "dev loop" and falls back to build-in-VM.
    """
    return os.environ.get(_WORKSPACE_DEFAULTS_OPT_IN_ENV_VAR) == "1"


def _operator_workspace_default(env_var: str, fallback: str) -> str:
    """Return ``env_var`` only when the operator explicitly opted in; else ``fallback``.

    The MINDS_WORKSPACE_GIT_URL / _BRANCH env vars wire the create-form
    defaults to the operator's local DEFAULT_WORKSPACE_TEMPLATE worktree. They are honored only when
    ``MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1`` is set in the same environment
    (``just minds-start`` and the e2e runner set it). An end-user ``minds run``
    never sets it, so a stray MINDS_WORKSPACE_* left in the shell is ignored on
    every tier -- the safety the previous tier-based gate provided, without also
    blocking dev iteration on staging / production.

    These defaults point at a *local* path and a dev branch, which only make
    sense for local-compute launch modes (Lima / Docker). For IMBUE_CLOUD (pool
    lease) they must not be kept -- a pool host cannot clone a local path and the
    dev branch matches no pre-baked host -- so the opt-in is the operator's
    signal that they are doing local dev iteration, not an end-user pool create.
    """
    if os.environ.get(_WORKSPACE_DEFAULTS_OPT_IN_ENV_VAR) != "1":
        return fallback
    return os.environ.get(env_var, fallback)


def default_workspace_template_ref() -> str:
    """Return the template ref a plain create uses -- the create form's Version default.

    For an end-user ``minds run`` this is always :data:`FALLBACK_BRANCH`, the
    ``minds-v*`` tag this binary was verified against; only an opted-in operator
    (``just minds-start``) sees the ``MINDS_WORKSPACE_BRANCH`` override, which is
    typically a branch name rather than a release tag.

    ``GET /api/v1/app/version`` also reports this as the ceiling on how far a
    workspace may update itself, so a branch value here imposes no ceiling.
    """
    return _operator_workspace_default("MINDS_WORKSPACE_BRANCH", FALLBACK_BRANCH)


def default_workspace_git_url() -> str:
    """Return the template repository a plain create uses -- the create form's Repository default.

    For an end-user ``minds run`` this is always the public
    default-workspace-template URL; only an opted-in operator
    (``just minds-start``) sees the ``MINDS_WORKSPACE_GIT_URL`` override,
    which points at their local DEFAULT_WORKSPACE_TEMPLATE worktree.
    """
    return _operator_workspace_default("MINDS_WORKSPACE_GIT_URL", _FALLBACK_GIT_URL)
