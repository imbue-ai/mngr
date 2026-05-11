from imbue.mngr_imbue_cloud.data_types import LeaseAttributes


def test_lease_attributes_drops_none_fields() -> None:
    attrs = LeaseAttributes(repo_url="https://example.com/repo.git", cpus=2)
    body = attrs.to_request_dict()
    assert body == {"repo_url": "https://example.com/repo.git", "cpus": 2}
    assert "memory_gb" not in body
    assert "gpu_count" not in body


def test_lease_attributes_empty_dict_when_unconstrained() -> None:
    assert LeaseAttributes().to_request_dict() == {}


def test_lease_attributes_includes_zero_values() -> None:
    # gpu_count=0 means "0 GPUs required", which is constraining and must be sent.
    attrs = LeaseAttributes(gpu_count=0)
    assert attrs.to_request_dict() == {"gpu_count": 0}
