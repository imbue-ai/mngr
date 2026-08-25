import pytest

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.mngr_imbue_cloud.primitives import CI_TIER
from imbue.mngr_imbue_cloud.primitives import DEV_TIER
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import InvalidImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import OVH_DATACENTER_CODE_BY_US_REGION
from imbue.mngr_imbue_cloud.primitives import PRODUCTION_TIER
from imbue.mngr_imbue_cloud.primitives import STAGING_TIER
from imbue.mngr_imbue_cloud.primitives import US_REGION_BY_OVH_DATACENTER_CODE
from imbue.mngr_imbue_cloud.primitives import WorkspaceId
from imbue.mngr_imbue_cloud.primitives import is_box_exclusive_to_tier
from imbue.mngr_imbue_cloud.primitives import slugify_account
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name


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


def test_tier_for_env_name_maps_the_shared_tiers_to_themselves() -> None:
    assert tier_for_env_name("production") == PRODUCTION_TIER
    assert tier_for_env_name("staging") == STAGING_TIER


def test_tier_for_env_name_maps_ci_prefixed_envs_to_the_ci_tier() -> None:
    assert tier_for_env_name("ci-20260518t140212z") == CI_TIER


def test_tier_for_env_name_maps_everything_else_to_the_dev_tier() -> None:
    assert tier_for_env_name("dev-josh") == DEV_TIER
    assert tier_for_env_name("dev-alice-3") == DEV_TIER
    # A "ci" substring that is not the tier prefix must not win.
    assert tier_for_env_name("dev-ci-leftover") == DEV_TIER


def test_is_box_exclusive_to_tier_requires_one_key_and_no_foreign_slices() -> None:
    assert is_box_exclusive_to_tier(authorized_key_count=1, foreign_tier_slice_count=0)
    # An extra key hands another tier SSH into the box; a missing one means prep never ran.
    assert not is_box_exclusive_to_tier(authorized_key_count=2, foreign_tier_slice_count=0)
    assert not is_box_exclusive_to_tier(authorized_key_count=0, foreign_tier_slice_count=0)
    assert not is_box_exclusive_to_tier(authorized_key_count=1, foreign_tier_slice_count=1)


def test_us_region_by_ovh_datacenter_code_round_trips_the_forward_map() -> None:
    # The reverse map is derived from the forward one; every pairing must survive
    # the round trip in both directions (which also proves neither side collides).
    assert len(US_REGION_BY_OVH_DATACENTER_CODE) == len(OVH_DATACENTER_CODE_BY_US_REGION)
    for region, datacenter in OVH_DATACENTER_CODE_BY_US_REGION.items():
        assert US_REGION_BY_OVH_DATACENTER_CODE[datacenter] == region


def test_workspace_id_accepts_a_services_agent_id() -> None:
    workspace_id = WorkspaceId("agent-0123456789abcdef0123456789abcdef")
    assert workspace_id == "agent-0123456789abcdef0123456789abcdef"


def test_workspace_id_rejects_host_shaped_and_malformed_values() -> None:
    with pytest.raises(InvalidRandomIdError):
        WorkspaceId("host-0123456789abcdef0123456789abcdef")
    with pytest.raises(InvalidRandomIdError):
        WorkspaceId("agent-nothex")
    with pytest.raises(InvalidRandomIdError):
        WorkspaceId("agent-0123")
