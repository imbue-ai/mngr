"""Unit coverage for the ``app.py`` expression that puts the device's condition on the wire.

One line, and it is the whole server -> client path for the environment signal:
the app-global frame the hub pages' notice reads and the per-machine surfaces
fall back to. The SPA's own suite cannot see it -- it feeds itself hand-made
payloads -- so without this, hard-wiring it to NONE leaves the Python suite
green and the notice band dead.
"""

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.app import _derive_ui_environment_message
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.testing import build_stub_connectivity_detector


def test_the_app_level_frame_reports_what_the_detector_last_measured(root_concurrency_group: ConcurrencyGroup) -> None:
    """A confirmed reading reaches the frame; an absent or unprobed detector reports NONE."""
    detector, _prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False)

    assert _derive_ui_environment_message(None).state is EnvironmentBlock.NONE
    assert _derive_ui_environment_message(detector).state is EnvironmentBlock.NONE, (
        "an unmeasured device must not be reported as a broken one"
    )
    detector.probe_now()
    assert _derive_ui_environment_message(detector).state is EnvironmentBlock.OFFLINE
