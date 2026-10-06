from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.share_targets import WHOLE_MACHINE_SERVICE
from imbue.minds.desktop_client.share_targets import resolve_share_target_display_names
from imbue.minds.desktop_client.share_targets import resolve_share_target_labels
from imbue.minds.desktop_client.share_targets import share_target_display_names
from imbue.minds.desktop_client.share_targets import share_target_labels
from imbue.minds.desktop_client.share_targets import split_share_targets
from imbue.mngr.primitives import AgentId

_AGENT_ID = AgentId("agent-" + "a" * 32)


def test_split_share_targets_filters_interfaces_and_reserved_prefixes() -> None:
    # owner-exec is the internal SSH-equivalent exec channel (authorized by
    # request signatures, never a share grant); like the chat/terminal/browser
    # interfaces it must never be offered as a per-app share target.
    app_services, whole = split_share_targets(
        ["system_interface", "web", "Terminal", "chats", "owner-exec", "host-abc", "my-app"]
    )

    assert whole == WHOLE_MACHINE_SERVICE
    assert app_services == ["web", "my-app"]


def test_split_share_targets_keeps_an_app_whose_name_is_not_a_hostname_label() -> None:
    """The link is built from the service's own origin label, not from its name, so a name that could never
    be a hostname label is still a target: dropping it hid the app from the panel with nothing said."""
    app_services, _whole = split_share_targets(["system_interface", "my_app", "Imbue HUD", "web"])

    assert app_services == ["my_app", "Imbue HUD", "web"]


def test_share_target_display_names_cover_the_apps_that_have_one() -> None:
    # The whole-machine target is deliberately absent: the panel names it for
    # what it grants, not after the shell app that serves it.
    display_names = share_target_display_names(
        ["files", "web"], {"files": "File Viewer", "system_interface": "Workspace", "unrendered": "Nope"}
    )

    assert display_names == {"files": "File Viewer"}


def test_resolve_share_target_display_names_reads_the_registrations() -> None:
    resolver = make_resolver_with_data(
        make_agents_json(_AGENT_ID),
        service_logs={
            str(_AGENT_ID): make_service_log(
                "system_interface", "http://127.0.0.1:9001", "system_interface-shl1", "Workspace"
            )
            + make_service_log("files", "http://127.0.0.1:9002", "files-f1", "File Viewer")
            + make_service_log("web", "http://127.0.0.1:9003", "web-w1")
        },
    )

    # ``web`` registered without a manifest, so it has no display name and the
    # panel falls back to its service name.
    assert resolve_share_target_display_names(resolver, _AGENT_ID) == {"files": "File Viewer"}


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
