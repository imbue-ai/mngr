import json

from imbue.minds.desktop_client.labeled_hosts import ListedHost
from imbue.minds.desktop_client.labeled_hosts import find_host_by_create_attempt_id_label
from imbue.minds.desktop_client.labeled_hosts import parse_hosts_listing
from imbue.minds.desktop_client.pending_create_attempts import read_create_attempt_id_label


def test_parse_hosts_listing_parses_valid_rows() -> None:
    payload = json.dumps(
        {
            "hosts": [
                {
                    "id": "host-1111",
                    "name": "foo",
                    "provider": "lima",
                    "state": "RUNNING",
                    "labels": {"create-attempt-id": "create-attempt-abc"},
                    "agents": [{"id": "agent-1", "name": "system-services"}],
                }
            ]
        }
    )
    hosts = parse_hosts_listing(payload)
    assert len(hosts) == 1
    assert hosts[0].id == "host-1111"
    assert hosts[0].labels == {"create-attempt-id": "create-attempt-abc"}
    assert hosts[0].agents[0].name == "system-services"


def test_parse_hosts_listing_returns_empty_for_malformed_document() -> None:
    assert parse_hosts_listing("{not json") == []
    assert parse_hosts_listing(json.dumps({"unexpected": True})) == []


def test_parse_hosts_listing_skips_unparseable_rows() -> None:
    payload = json.dumps(
        {
            "hosts": [
                {"id": "host-good", "name": "ok", "provider": "docker"},
                {"name": "missing-id"},
            ]
        }
    )
    hosts = parse_hosts_listing(payload)
    assert [host.id for host in hosts] == ["host-good"]


def test_find_host_by_create_attempt_id_label_matches_the_labeled_host() -> None:
    hosts = [
        ListedHost(id="host-1", name="one", provider="lima", labels={"create-attempt-id": "create-attempt-one"}),
        ListedHost(id="host-2", name="two", provider="lima", labels={"create-attempt-id": "create-attempt-two"}),
        ListedHost(id="host-3", name="three", provider="lima", labels={}),
    ]
    found = find_host_by_create_attempt_id_label(hosts, "create-attempt-two")
    assert found is not None
    assert found.id == "host-2"
    assert find_host_by_create_attempt_id_label(hosts, "create-attempt-missing") is None


def test_find_host_by_create_attempt_id_label_skips_terminal_host_records() -> None:
    hosts = [
        ListedHost(id="host-dead", name="dead", provider="lima", state="FAILED", labels={"create-attempt-id": "c-1"}),
        ListedHost(
            id="host-destroyed", name="gone", provider="lima", state="DESTROYED", labels={"create-attempt-id": "c-1"}
        ),
    ]
    assert find_host_by_create_attempt_id_label(hosts, "c-1") is None


def test_find_host_by_create_attempt_id_label_reads_the_legacy_workspace_id_label() -> None:
    hosts = [
        ListedHost(id="host-old", name="old", provider="lima", labels={"workspace-id": "create-attempt-legacy"}),
    ]
    found = find_host_by_create_attempt_id_label(hosts, "create-attempt-legacy")
    assert found is not None
    assert found.id == "host-old"


def test_read_create_attempt_id_label_prefers_the_new_label() -> None:
    labels = {"create-attempt-id": "create-attempt-new", "workspace-id": "create-attempt-old"}
    assert read_create_attempt_id_label(labels) == "create-attempt-new"
    assert read_create_attempt_id_label({}) is None
