from imbue.mngr.utils.paths import resolve_shipped_path


def test_resolve_shipped_path_returns_packaged_copy() -> None:
    # main.py lives under the package dir (imbue/mngr) in every install, so it resolves there.
    resolved = resolve_shipped_path("main.py")
    assert resolved is not None
    assert resolved.parts[-2:] == ("mngr", "main.py")


def test_resolve_shipped_path_falls_back_to_source_tree() -> None:
    # constraints.txt is force-included into the wheel but in a checkout only exists under libs/mngr.
    resolved = resolve_shipped_path("constraints.txt")
    assert resolved is not None
    assert resolved.parts[-3:] == ("libs", "mngr", "constraints.txt")


def test_resolve_shipped_path_returns_none_when_absent() -> None:
    assert resolve_shipped_path("this_file_does_not_exist.xyz") is None
