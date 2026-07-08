from imbue.minds.desktop_client.provider_display import friendly_provider_label


def test_friendly_provider_label_collapses_per_region_aws() -> None:
    assert friendly_provider_label("aws-us-east-1") == "AWS"
    assert friendly_provider_label("aws-eu-central-1") == "AWS"
    assert friendly_provider_label("aws") == "AWS"


def test_friendly_provider_label_collapses_per_account_imbue_cloud() -> None:
    assert friendly_provider_label("imbue_cloud_alice-imbue-com") == "Imbue Cloud"
    assert friendly_provider_label("imbue_cloud") == "Imbue Cloud"


def test_friendly_provider_label_known_exact_providers() -> None:
    assert friendly_provider_label("docker") == "Docker"
    assert friendly_provider_label("lima") == "Lima"
    assert friendly_provider_label("vultr") == "Vultr"
    assert friendly_provider_label("ovh") == "OVH"


def test_friendly_provider_label_empty_for_unknown_or_none() -> None:
    assert friendly_provider_label(None) == ""
    assert friendly_provider_label("") == ""


def test_friendly_provider_label_falls_back_to_raw_name_for_unknown_provider() -> None:
    # A provider we don't have a friendly label for is still shown verbatim
    # rather than hidden, so a newly-added backend remains visible in the UI.
    assert friendly_provider_label("some-future-provider") == "some-future-provider"
