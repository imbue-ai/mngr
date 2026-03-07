"""Test fixtures for mng-kanpan.

Uses shared plugin test fixtures from mng for common setup (plugin manager,
environment isolation, git repos, temp_mng_ctx, local_provider, etc.).
"""

from types import SimpleNamespace

from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


class CallTracker:
    """Lightweight call tracker to replace MagicMock.assert_called patterns.

    Uses __new__ + class-level default instead of __init__ to satisfy the
    no-init-in-non-exception ratchet.
    """

    call_count: int

    def __new__(cls) -> "CallTracker":
        instance = super().__new__(cls)
        instance.call_count = 0
        return instance

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.call_count += 1


def make_mock_loop() -> SimpleNamespace:
    """Create a lightweight loop substitute with a trackable set_alarm_in."""
    tracker = CallTracker()
    return SimpleNamespace(set_alarm_in=tracker, _alarm_tracker=tracker)
