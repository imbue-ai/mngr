import pytest

from imbue.minds.smoke_tests.app_flows import is_workspace_gone
from imbue.minds.smoke_tests.app_flows import share_url_for_service

_SHARING_STATUS = {
    "host_id": "host-0123456789abcdef",
    "enabled": True,
    "url": "https://ws-ab12cd.personal-imbue-staging.com/",
    "service_labels": {"system_interface": "shell-x1y2", "hello": "hello-q9w8"},
}


def test_a_service_s_share_link_is_its_label_on_the_share_s_domain() -> None:
    assert (
        share_url_for_service(_SHARING_STATUS, "hello") == "https://hello-q9w8.ws-ab12cd.personal-imbue-staging.com/"
    )


@pytest.mark.parametrize(
    "status",
    [
        {**_SHARING_STATUS, "service_labels": {"system_interface": "shell-x1y2"}},
        {**_SHARING_STATUS, "url": None},
        {**_SHARING_STATUS, "service_labels": None},
        {"enabled": False},
    ],
)
def test_a_share_without_a_url_or_without_the_service_s_label_has_no_link_yet(status: dict[str, object]) -> None:
    assert share_url_for_service(status, "hello") is None


def test_a_workspace_is_gone_when_unlisted_or_listed_without_a_live_host() -> None:
    agent_id = "agent-0123456789abcdef0123456789abcdef"
    assert is_workspace_gone([], agent_id) is True
    assert is_workspace_gone([{"agent_id": "agent-other", "host_state": "RUNNING"}], agent_id) is True
    assert is_workspace_gone([{"agent_id": agent_id, "host_state": None}], agent_id) is True
    assert is_workspace_gone([{"agent_id": agent_id, "host_state": "DESTROYED"}], agent_id) is True
    assert is_workspace_gone([{"agent_id": agent_id, "host_state": "RUNNING"}], agent_id) is False
    assert is_workspace_gone([{"agent_id": agent_id, "host_state": "STOPPING"}], agent_id) is False
