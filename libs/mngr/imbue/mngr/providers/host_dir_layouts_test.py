from pathlib import Path

from imbue.mngr.providers.host_dir_layouts import KNOWN_WORKSPACE_HOST_DIRS
from imbue.mngr.providers.host_dir_layouts import host_dir_fallbacks


def test_fallbacks_run_in_both_directions() -> None:
    """A client on either generation must be able to read hosts of the other.

    The first version of this only fell back new -> old, so a client whose
    config resolved the old layout (the built-in default, which is what a
    ``$HOME``-scoped read gets when no project settings apply) could not read a
    host baked under the new one -- the exact mirror of the bug it fixed.
    """
    assert "/mngr" in host_dir_fallbacks("/home/user/.mngr")
    assert "/home/user/.mngr" in host_dir_fallbacks("/mngr")


def test_the_configured_layout_is_never_probed_twice() -> None:
    """The caller puts its configured value at the head of the candidate list itself."""
    for configured in KNOWN_WORKSPACE_HOST_DIRS:
        assert configured not in host_dir_fallbacks(configured)


def test_an_unknown_configured_layout_still_gets_every_known_one() -> None:
    """A custom host_dir is a real configuration, not a reason to stop probing.

    It leads its own candidate list, so nothing is lost by also offering the
    layouts mngr bakes -- a host created under one of them is still readable.
    """
    assert host_dir_fallbacks("/opt/custom/mngr") == KNOWN_WORKSPACE_HOST_DIRS


def test_a_path_configured_value_is_accepted() -> None:
    """Provider configs hold ``Path``; the script builders want ``str`` candidates."""
    assert host_dir_fallbacks(Path("/home/user/.mngr")) == host_dir_fallbacks("/home/user/.mngr")
