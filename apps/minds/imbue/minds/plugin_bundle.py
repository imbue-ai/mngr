"""Which mngr plugins this (curated) minds bundle ships vs. omits.

minds packages a deliberate subset of mngr's plugin catalog. Synced config
(e.g. an imbue_cloud account whose pool offers an AWS-backed host) can still
reference a provider backend whose plugin the bundle did not install. mngr's
strict config loader aborts on a `[providers.<name>]` block referencing an
unregistered backend, so the bundle declares the plugins it did not install as
disabled, letting the loader skip those blocks instead.
"""

import importlib.metadata

from imbue.mngr.plugin_catalog import get_all_cataloged_entry_point_names

_MNGR_ENTRY_POINT_GROUP = "mngr"


def installed_mngr_plugin_names() -> frozenset[str]:
    """Entry-point names of the mngr plugins actually installed in this bundle."""
    return frozenset(ep.name for ep in importlib.metadata.entry_points(group=_MNGR_ENTRY_POINT_GROUP))


def unbundled_plugin_names() -> frozenset[str]:
    """Cataloged mngr plugins this bundle did not install.

    Disabling these in mngr's config makes a `[providers.<name>]` block that
    references one of them a skipped no-op rather than a fatal
    "references unknown backend" config error.
    """
    return get_all_cataloged_entry_point_names() - installed_mngr_plugin_names()
