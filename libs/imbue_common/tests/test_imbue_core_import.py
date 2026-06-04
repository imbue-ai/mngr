from imbue import imbue_common


def test_imbue_common_package_is_importable() -> None:
    # The module-level import above fails at collection time if the imbue.imbue_common
    # package cannot be imported; this asserts it resolved to the expected package.
    assert imbue_common.__name__ == "imbue.imbue_common"
