import pytest


def patch_platform(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    """Set a fake platform.system() in the notifier module."""
    monkeypatch.setattr("imbue.mngr_notifications.notifier.platform.system", lambda: system)
