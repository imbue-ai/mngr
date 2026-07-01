import pytest

from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import Sha256Hex
from imbue.minds.lima_image.primitives import lima_provider_image_url_setting_key


def test_sha256hex_accepts_and_normalizes_valid_digest() -> None:
    raw = "AB" * 32
    assert Sha256Hex(raw) == raw.lower()


@pytest.mark.parametrize("bad", ["", "xyz", "a" * 63, "a" * 65, "g" * 64])
def test_sha256hex_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        Sha256Hex(bad)


def test_setting_key_matches_provider_config_fields() -> None:
    assert lima_provider_image_url_setting_key(ImageArch.AARCH64) == "providers.lima.default_image_url_aarch64"
    assert lima_provider_image_url_setting_key(ImageArch.X86_64) == "providers.lima.default_image_url_x86_64"
