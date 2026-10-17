"""Shared account-choice vocabulary for the latchkey permission dialogs.

The predefined-permission detail payload offers the user a pick-list of
accounts the grant can attach to; the constants and the
:class:`PermissionAccountChoice` shape here define that vocabulary. The SPA
renders the dialog from the typed payload (see ``request_handler.py``) and
submits the chosen value back to the grant route, which resolves it against
the same constants.
"""

from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_latchkey.account_scopes import ACCOUNT_SCOPE_SEPARATOR

# The catch-all ``any`` permission is stored and submitted verbatim (it is
# Detent's wildcard schema), but users find ``all`` clearer, so the dialog
# shows this label in its place while the underlying checkbox value stays
# ``any``. Public: the typed inbox-detail payload carries it to the SPA.
WILDCARD_PERMISSION_UI_LABEL: Final[str] = "all"

# Form value of the predefined dialog's "sign a new account in" choice. Chosen
# to be implausible as a real account name, but the grant flow does not rely on
# that: it resolves the submitted value against the service's stored accounts
# first and only falls back to the sign-in flow when nothing matches, so even an
# account literally named this would still be treated as the account it is.
NEW_ACCOUNT_FORM_VALUE: Final[str] = f"{ACCOUNT_SCOPE_SEPARATOR}new-account"

# Label for latchkey's single unnamed "default" account (keyed by the empty
# string). Mirrors the connectors settings page so the same account reads the
# same way in both places.
DEFAULT_ACCOUNT_LABEL: Final[str] = "Default account"


class PermissionAccountChoice(FrozenModel):
    """One selectable account in the predefined permission dialog."""

    value: str = Field(
        description=(
            'Form value: the latchkey account key (``""`` for the unnamed default) or '
            ":data:`NEW_ACCOUNT_FORM_VALUE` for the sign-in-a-new-account choice."
        ),
    )
    label: str = Field(description="User-facing account label.")
    hint: str = Field(default="", description="Short qualifier shown next to the label (e.g. 'needs sign-in').")
    is_credential_setup_needed: bool = Field(
        description="Whether picking this account has to establish credentials before the grant can apply.",
    )
    is_account_name_needed: bool = Field(
        description="Whether picking this account also requires the user to name it (manual-credentials services).",
    )
