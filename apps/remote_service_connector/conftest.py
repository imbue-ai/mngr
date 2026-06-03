import pytest

import imbue.remote_service_connector.app as app_mod
from imbue.imbue_common.conftest_hooks import register_conftest_hooks

register_conftest_hooks(globals())


@pytest.fixture(autouse=True)
def _clear_paid_status_cache() -> None:
    """Drop the connector's process-global paid-status cache before each test.

    The cache lives at module scope, so without this a positive/negative
    entry from one test could bleed into another. Tests that exercise the
    cache set a positive TTL explicitly; everything else runs with it empty.
    """
    app_mod.clear_paid_status_cache()
