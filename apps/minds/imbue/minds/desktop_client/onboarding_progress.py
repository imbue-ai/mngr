"""Whether this installation has been taken past the first-run start flow.

The Electron startup router and the SPA's home page both ask one question --
"should a launch with no workspaces land on the start flow?" -- and this module
is the single answer. The flag lives in ``config.toml`` (:class:`MindsConfig`)
and is written by the create front door when an attempt starts and by the
onboarding ``complete`` route when the user signs in from the start flow.
"""

from loguru import logger

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.minds_config import MindsConfig


def resolve_is_onboarding_complete(
    minds_config: MindsConfig | None,
    backend_resolver: BackendResolverInterface,
) -> bool:
    """The onboarding-complete verdict, converging the flag for installs that predate it.

    With no config store wired (minimal test apps) the answer is True, so such
    an app never routes to the start flow.
    """
    if minds_config is None:
        return True
    if minds_config.get_is_onboarding_complete():
        return True
    # CLEANUP: drop this backfill once every supported install has launched a
    # build that writes the flag from the create front door. An install that
    # created its workspaces on an older build has no flag, and must not be
    # sent through the start flow the day it destroys its last workspace.
    if backend_resolver.list_active_workspace_ids():
        logger.debug("Backfilled the onboarding-complete flag from an existing workspace")
        minds_config.set_is_onboarding_complete(True)
        return True
    return False
