from imbue.minds.desktop_client.api_transport import ApiTransport
from imbue.minds.desktop_client.api_transport import LOCAL_TRUSTED_ENVIRON_KEY
from imbue.minds.desktop_client.api_transport import agent_label_from_environ
from imbue.minds.desktop_client.api_transport import is_local_trusted_environ
from imbue.minds.desktop_client.api_transport import mark_requests_local_trusted
from imbue.minds.desktop_client.api_transport import transport_for_environ


def test_only_the_exact_true_marker_counts_as_local_trusted() -> None:
    assert is_local_trusted_environ({LOCAL_TRUSTED_ENVIRON_KEY: True})
    assert not is_local_trusted_environ({LOCAL_TRUSTED_ENVIRON_KEY: "true"})
    assert not is_local_trusted_environ({LOCAL_TRUSTED_ENVIRON_KEY: 1})
    assert not is_local_trusted_environ({})
    # A header cannot spell the marker: headers arrive as HTTP_* keys.
    assert not is_local_trusted_environ({"HTTP_IMBUE_STUDIO_IS_LOCAL_TRUSTED": "True"})


def test_transport_for_environ() -> None:
    assert transport_for_environ({}) is ApiTransport.GATEWAY
    assert transport_for_environ({LOCAL_TRUSTED_ENVIRON_KEY: True}) is ApiTransport.LOCAL_SOCKET


def test_agent_label_is_read_from_its_header_and_blank_values_are_none() -> None:
    assert agent_label_from_environ({"HTTP_X_IMBUE_STUDIO_AGENT": " deploy-bot "}) == "deploy-bot"
    assert agent_label_from_environ({"HTTP_X_IMBUE_STUDIO_AGENT": "   "}) is None
    assert agent_label_from_environ({}) is None


def test_mark_requests_local_trusted_sets_the_marker_before_calling_the_app() -> None:
    seen: list[dict[str, object]] = []

    def inner(environ: dict[str, object], start_response: object) -> list[bytes]:
        seen.append(dict(environ))
        return [b"ok"]

    wrapped = mark_requests_local_trusted(inner)
    body = wrapped({"REQUEST_METHOD": "GET", "PATH_INFO": "/api/v1/app/version"}, lambda *_: None)

    assert list(body) == [b"ok"]
    assert seen[0][LOCAL_TRUSTED_ENVIRON_KEY] is True
