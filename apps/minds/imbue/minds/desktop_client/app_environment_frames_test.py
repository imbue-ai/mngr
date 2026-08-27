"""Unit coverage for the ``app.py`` expression that puts the device's condition on the wire.

One line, and it is the whole server -> client path for the environment signal:
the app-global frame the hub pages' notice reads and the per-machine surfaces
fall back to. The SPA's own suite cannot see it -- it feeds itself hand-made
payloads -- so without this, hard-wiring it to NONE leaves the Python suite
green and the notice band dead.
"""

from datetime import datetime
from datetime import timezone

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.app import _derive_ui_environment_message
from imbue.minds.desktop_client.environment_signals import EnvironmentCondition
from imbue.minds.desktop_client.testing import build_stub_connectivity_detector


@pytest.mark.witnesses(
    "no-blame-past-an-unmeasured-device",
    partial="witnesses that an unmeasured device reaches the surfaces as such; what they withhold on it is witnessed by the SPA's own suite",
)
def test_the_app_level_frame_reports_what_the_detector_last_measured(root_concurrency_group: ConcurrencyGroup) -> None:
    """A confirmed reading reaches the frame; an unprobed or wake-blanked detector reports UNKNOWN, not NONE.

    NONE is the answer for acting, where no evidence must withhold nothing. On
    the wire it would read as "the device is fine", and a surface told that goes
    on to blame the provider -- which after a wake is exactly the wrong headline.
    """
    detector, _prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False)

    assert _derive_ui_environment_message(None).state is EnvironmentCondition.NONE
    assert _derive_ui_environment_message(detector).state is EnvironmentCondition.UNKNOWN, (
        "an unmeasured device must be reported as neither broken nor fine"
    )
    detector.probe_now()
    assert _derive_ui_environment_message(detector).state is EnvironmentCondition.OFFLINE
    detector.invalidate_after_wake(datetime.now(timezone.utc))
    assert _derive_ui_environment_message(detector).state is EnvironmentCondition.UNKNOWN
