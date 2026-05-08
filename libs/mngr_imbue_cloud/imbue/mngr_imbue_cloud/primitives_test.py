import pytest

from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import InvalidImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import slugify_account


def test_account_lowercases_and_strips() -> None:
    account = ImbueCloudAccount(" Alice@Imbue.COM ")
    assert account == "alice@imbue.com"


def test_account_rejects_invalid_emails() -> None:
    with pytest.raises(InvalidImbueCloudAccount):
        ImbueCloudAccount("not-an-email")
    with pytest.raises(InvalidImbueCloudAccount):
        ImbueCloudAccount("alice@@imbue.com")
    with pytest.raises(InvalidImbueCloudAccount):
        ImbueCloudAccount("")


def test_slugify_account_is_filesystem_safe() -> None:
    slug = slugify_account("Alice.Bob+test@imbue.com")
    assert slug == "alice-bob-test-imbue-com"
    assert "@" not in slug


def test_slugify_account_rejects_pure_punctuation() -> None:
    with pytest.raises(InvalidImbueCloudAccount):
        slugify_account("@@@")
