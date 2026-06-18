from imbue.mngr_vps.systemd import render_systemd_unit


def test_renders_sections_and_entries_in_order() -> None:
    unit = render_systemd_unit(
        {
            "Unit": [("Description", "test unit")],
            "Service": [("Type", "oneshot"), ("ExecStart", "/usr/local/sbin/do.sh")],
        }
    )
    assert unit == "[Unit]\nDescription=test unit\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/do.sh\n"


def test_allows_repeated_keys() -> None:
    unit = render_systemd_unit(
        {
            "Service": [
                ("Environment", "A=1"),
                ("Environment", "B=2"),
                ("ExecStart", "/usr/local/sbin/do.sh"),
            ]
        }
    )
    assert unit == "[Service]\nEnvironment=A=1\nEnvironment=B=2\nExecStart=/usr/local/sbin/do.sh\n"


def test_renders_a_section_with_no_entries() -> None:
    assert render_systemd_unit({"Install": []}) == "[Install]\n"
