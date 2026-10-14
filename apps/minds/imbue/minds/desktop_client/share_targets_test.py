from imbue.minds.desktop_client.backend_resolver import ServiceShareKind
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


def test_split_share_targets_sorts_a_legacy_workspaces_services_by_name() -> None:
    # A workspace that announces no share kind falls back to the name lists.
    targets = split_share_targets(
        [
            "system_interface",
            "web",
            "Terminal",
            "chats",
            "owner-exec",
            "vm-exec",
            "terminal-pty",
            "getting-started",
            "host-abc",
            "my-app",
        ],
        {},
    )

    assert targets.whole_service == WHOLE_MACHINE_SERVICE
    assert targets.app_services == ("web", "my-app")
    assert targets.system_services == ("Terminal", "chats", "getting-started")


def test_split_share_targets_follows_the_announced_share_kind_over_the_name() -> None:
    targets = split_share_targets(
        ["chat", "terminal", "metrics", "notes", "files"],
        {
            "chat": ServiceShareKind.APP,
            "terminal": ServiceShareKind.INTERNAL,
            "metrics": ServiceShareKind.SYSTEM,
            "notes": ServiceShareKind.APP,
        },
    )

    assert targets.app_services == ("chat", "notes", "files")
    assert targets.system_services == ("metrics",)


def test_split_share_targets_never_offers_the_whole_machine_or_a_coordinate_on_its_own() -> None:
    targets = split_share_targets(
        ["system_interface", "host-abc", "web"],
        {"system_interface": ServiceShareKind.APP, "host-abc": ServiceShareKind.SYSTEM},
    )

    assert targets.per_app_services == ("web",)


def test_split_share_targets_keeps_an_app_whose_name_is_not_a_hostname_label() -> None:
    """The link is built from the service's own origin label, not from its name, so a name that could never
    be a hostname label is still a target: dropping it hid the app from the panel with nothing said."""
    targets = split_share_targets(["system_interface", "my_app", "Imbue HUD", "web"], {})

    assert targets.app_services == ("my_app", "Imbue HUD", "web")


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


def test_resolve_share_target_labels_covers_system_apps_but_not_internal_services() -> None:
    # The web app has registered but its label has not reached this client, and
    # the terminal's pty is internal, not a share target: neither gets a label,
    # so neither can be rendered as a link. The terminal is a system app, which
    # the Share tab still lists, so it needs its link.
    resolver = make_resolver_with_data(
        make_agents_json(_AGENT_ID),
        service_logs={
            str(_AGENT_ID): make_service_log("system_interface", "http://127.0.0.1:9001", "system_interface-shl1")
            + make_service_log("web", "http://127.0.0.1:9002")
            + make_service_log("terminal", "http://127.0.0.1:9003", "terminal-t1")
            + make_service_log("terminal-pty", "http://127.0.0.1:9004", "terminal-pty-p1")
        },
    )

    assert resolve_share_target_labels(resolver, _AGENT_ID) == {
        "system_interface": "system_interface-shl1",
        "terminal": "terminal-t1",
    }


def test_resolve_share_target_labels_is_empty_before_any_registration_arrives() -> None:
    resolver = make_resolver_with_data(make_agents_json(_AGENT_ID))

    assert resolve_share_target_labels(resolver, _AGENT_ID) == {}
