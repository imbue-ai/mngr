"""Shared display rules for latchkey accounts.

The predefined-permission dialog (:mod:`.handlers.predefined`) and the
connectors settings page (:mod:`.permission_overview`) must present a
service's accounts identically: the unnamed default account reads as
"Default account" and sorts last; named accounts sort case-insensitively.
This module is the single owner of that contract.
"""

from typing import Final

from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT

# Label shown for a service's single unnamed "default" account (latchkey keys
# it by the empty string). Users never typed a name for it, so we show a
# neutral placeholder rather than an empty row.
DEFAULT_ACCOUNT_LABEL: Final[str] = "Default account"


def account_label(account: str) -> str:
    """Render a latchkey account key as a user-facing label (the default one is unnamed)."""
    return DEFAULT_ACCOUNT_LABEL if account == DEFAULT_ACCOUNT else account


def account_display_sort_key(account: str) -> tuple[bool, str]:
    """Sort key ordering accounts for display: named ones alphabetically, the unnamed default last."""
    return (account == DEFAULT_ACCOUNT, account.lower())
