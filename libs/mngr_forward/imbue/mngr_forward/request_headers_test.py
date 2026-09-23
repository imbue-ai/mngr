import json
from pathlib import Path

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.request_headers import DEFAULT_AGENT_KEY
from imbue.mngr_forward.request_headers import RequestHeadersFile
from imbue.mngr_forward.request_headers import RequestHeadersFileReader

_AGENT_A = str(AgentId.generate())
_AGENT_B = str(AgentId.generate())


def test_headers_for_agent_prefers_the_agents_entry_over_the_default() -> None:
    headers_file = RequestHeadersFile(
        headers_by_agent_key={
            DEFAULT_AGENT_KEY: {"X-Example-Requester": "anyone", "X-Example-Tier": "default"},
            _AGENT_A: {"X-Example-Requester": "alice"},
        }
    )

    own = headers_file.headers_for_agent(_AGENT_A)
    assert own.values_by_name == {"X-Example-Requester": "alice"}
    default = headers_file.headers_for_agent(_AGENT_B)
    assert default.values_by_name == {"X-Example-Requester": "anyone", "X-Example-Tier": "default"}
    # The strip set is the union over every entry, lowercased, for every agent.
    assert own.names_to_strip == default.names_to_strip == frozenset({"x-example-requester", "x-example-tier"})


def test_headers_for_agent_strips_but_sets_nothing_without_a_matching_or_default_entry() -> None:
    headers_file = RequestHeadersFile(headers_by_agent_key={_AGENT_A: {"X-Example-Requester": "alice"}})

    unlisted = headers_file.headers_for_agent(_AGENT_B)

    assert unlisted.values_by_name == {}
    assert unlisted.names_to_strip == frozenset({"x-example-requester"})


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"content-length": "0"},
        {"Connection": "close"},
        {"Transfer-Encoding": "chunked"},
        {"Upgrade": "h2c"},
        {"Bad Name": "x"},
        {"": "x"},
    ],
)
def test_file_rejects_framing_hop_by_hop_and_malformed_header_names(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        RequestHeadersFile(headers_by_agent_key={DEFAULT_AGENT_KEY: headers})


def test_file_rejects_keys_that_are_neither_an_agent_id_nor_the_default() -> None:
    with pytest.raises(ValueError):
        RequestHeadersFile(headers_by_agent_key={"agent-1": {"X-Example": "x"}})
    with pytest.raises(ValueError):
        RequestHeadersFile(headers_by_agent_key={"host-" + "a" * 32: {"X-Example": "x"}})


def test_reader_serves_nothing_for_a_missing_file(tmp_path: Path) -> None:
    reader = RequestHeadersFileReader(path=tmp_path / "absent.json")
    resolved = reader.headers_for_agent(_AGENT_A)
    assert resolved.values_by_name == {}
    assert resolved.names_to_strip == frozenset()


def test_reader_reloads_when_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "request_headers.json"
    path.write_text(json.dumps({_AGENT_A: {"X-Example-Requester": "alice"}}))
    reader = RequestHeadersFileReader(path=path)
    assert reader.headers_for_agent(_AGENT_A).values_by_name == {"X-Example-Requester": "alice"}
    assert reader.headers_for_agent(_AGENT_B).values_by_name == {}

    # A rewrite with a different size is picked up on the next lookup
    # regardless of mtime granularity.
    path.write_text(json.dumps({_AGENT_B: {"X-Example-Requester": "bea", "X-Example-Tier": "gold"}}))
    assert reader.headers_for_agent(_AGENT_A).values_by_name == {}
    assert reader.headers_for_agent(_AGENT_B).values_by_name == {
        "X-Example-Requester": "bea",
        "X-Example-Tier": "gold",
    }

    path.unlink()
    assert reader.headers_for_agent(_AGENT_B).values_by_name == {}


def test_reader_treats_a_malformed_file_as_empty_until_it_changes(tmp_path: Path) -> None:
    path = tmp_path / "request_headers.json"
    path.write_text("{not json")
    reader = RequestHeadersFileReader(path=path)
    assert reader.headers_for_agent(_AGENT_A).values_by_name == {}

    # A reserved header name makes the whole file malformed: nothing from it is applied.
    path.write_text(json.dumps({_AGENT_A: {"X-Example-Requester": "alice", "Host": "evil.example"}}))
    resolved = reader.headers_for_agent(_AGENT_A)
    assert resolved.values_by_name == {}
    assert resolved.names_to_strip == frozenset()

    # So does a non-string header value.
    path.write_text(json.dumps({_AGENT_A: {"X-Example-Requester": 1}}))
    assert reader.headers_for_agent(_AGENT_A).values_by_name == {}

    path.write_text(json.dumps({_AGENT_A: {"X-Example-Requester": "alice"}}))
    assert reader.headers_for_agent(_AGENT_A).values_by_name == {"X-Example-Requester": "alice"}


def test_reader_serves_nothing_when_the_file_cannot_be_stated(tmp_path: Path) -> None:
    # A parent that is a regular file makes stat fail with NotADirectoryError,
    # which must degrade to "no edits" rather than fail every proxied request.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    reader = RequestHeadersFileReader(path=blocker / "request_headers.json")
    assert reader.headers_for_agent(_AGENT_A).values_by_name == {}
    assert reader.headers_for_agent(_AGENT_A).values_by_name == {}
