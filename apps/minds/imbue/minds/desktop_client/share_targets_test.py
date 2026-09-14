from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.share_targets import WHOLE_MACHINE_SERVICE
from imbue.minds.desktop_client.share_targets import resolve_share_target_labels
from imbue.minds.desktop_client.share_targets import share_target_labels
from imbue.minds.desktop_client.share_targets import split_share_targets
from imbue.mngr.primitives import AgentId

_AGENT_ID = AgentId("agent-" + "a" * 32)


def test_split_share_targets_filters_interfaces_and_non_dns_names() -> None:
    # owner-exec is the internal SSH-equivalent exec channel (authorized by
    # request signatures, never a share grant); like the chat/terminal/browser
    # interfaces it must never be offered as a per-app share target.
    app_services, whole = split_share_targets(
        ["system_interface", "web", "Terminal", "chats", "owner-exec", "bad_name", "host-abc", "my-app"]
    )

    assert whole == WHOLE_MACHINE_SERVICE
    assert app_services == ["web", "my-app"]


def test_share_target_labels_cover_targets_and_shell_only() -> None:
    labels = share_target_labels(
        ["web"], {"web": "web-r4nd", "system_interface": "shell-r4nd", "unrendered": "u-r4nd"}
    )

    assert labels == {"web": "web-r4nd", "system_interface": "shell-r4nd"}


def test_resolve_share_target_labels_omits_targets_whose_label_is_not_known() -> None:
    # The web app has registered but its label has not reached this client, and
    # the terminal is an interface, not a share target: neither gets a label,
    # so neither can be rendered as a link.
    resolver = make_resolver_with_data(
        make_agents_json(_AGENT_ID),
        service_logs={
            str(_AGENT_ID): make_service_log("system_interface", "http://127.0.0.1:9001", "system_interface-shl1")
            + make_service_log("web", "http://127.0.0.1:9002")
            + make_service_log("terminal", "http://127.0.0.1:9003", "terminal-t1")
        },
    )

    assert resolve_share_target_labels(resolver, _AGENT_ID) == {"system_interface": "system_interface-shl1"}


def test_resolve_share_target_labels_is_empty_before_any_registration_arrives() -> None:
    resolver = make_resolver_with_data(make_agents_json(_AGENT_ID))

    assert resolve_share_target_labels(resolver, _AGENT_ID) == {}
