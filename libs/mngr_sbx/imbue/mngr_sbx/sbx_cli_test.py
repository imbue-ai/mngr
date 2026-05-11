"""Unit tests for the pure parsers in sbx_cli.

The subprocess-driving paths are exercised by integration tests guarded by the
sbx CLI being installed; these tests cover only the string-parsing helpers.
"""

from imbue.mngr_sbx.sbx_cli import SbxPortBinding
from imbue.mngr_sbx.sbx_cli import _extract_sandbox_records
from imbue.mngr_sbx.sbx_cli import _parse_port_listing


def test_parse_port_listing_handles_sandbox_to_host_arrow() -> None:
    output = "22/tcp -> 127.0.0.1:32769\n"
    result = _parse_port_listing(output)
    assert result == [
        SbxPortBinding(sandbox_port=22, host_ip="127.0.0.1", host_port=32769, protocol="tcp"),
    ]


def test_parse_port_listing_handles_docker_style_arrow() -> None:
    output = "127.0.0.1:32770->22/tcp\n"
    result = _parse_port_listing(output)
    assert result == [
        SbxPortBinding(sandbox_port=22, host_ip="127.0.0.1", host_port=32770, protocol="tcp"),
    ]


def test_parse_port_listing_handles_published_prefix_from_publish_flag() -> None:
    # This is the actual format that 'sbx ports --publish 22' emits in sbx 0.28.x.
    output = "Published 127.0.0.1:49153 -> 22/tcp\n"
    result = _parse_port_listing(output)
    assert result == [
        SbxPortBinding(sandbox_port=22, host_ip="127.0.0.1", host_port=49153, protocol="tcp"),
    ]


def test_parse_port_listing_handles_table_format_from_bare_ports() -> None:
    # This is the actual format that 'sbx ports <name>' (no --publish) emits.
    output = "HOST IP     HOST PORT   SANDBOX PORT   PROTOCOL\n127.0.0.1   49153       22             tcp\n"
    result = _parse_port_listing(output)
    assert result == [
        SbxPortBinding(sandbox_port=22, host_ip="127.0.0.1", host_port=49153, protocol="tcp"),
    ]


def test_parse_port_listing_handles_multiple_lines() -> None:
    output = "22/tcp -> 127.0.0.1:32769\n8080/tcp -> 127.0.0.1:32770\n"
    result = _parse_port_listing(output)
    assert {b.sandbox_port for b in result} == {22, 8080}


def test_parse_port_listing_skips_unrecognized_lines() -> None:
    output = "Port mappings:\n22/tcp -> 127.0.0.1:32769\nno ports defined\nbogus line\n"
    result = _parse_port_listing(output)
    assert len(result) == 1
    assert result[0].sandbox_port == 22


def test_parse_port_listing_empty_returns_empty_list() -> None:
    assert _parse_port_listing("") == []


def test_parse_port_listing_skips_table_header_lines() -> None:
    output = "Name    Status\nMy sbx  running\n22/tcp -> 127.0.0.1:32769\n"
    result = _parse_port_listing(output)
    assert len(result) == 1
    assert result[0].sandbox_port == 22


def test_parse_port_listing_handles_udp_protocol() -> None:
    output = "53/udp -> 127.0.0.1:5353\n"
    result = _parse_port_listing(output)
    assert result[0].protocol == "udp"
    assert result[0].sandbox_port == 53


def test_extract_sandbox_records_unwraps_sandboxes_key() -> None:
    # sbx 0.28.x emits {"sandboxes": [...]} -- ensure we flatten it.
    parsed = {"sandboxes": [{"name": "a", "agent": "shell"}, {"name": "b", "agent": "claude"}]}
    records = _extract_sandbox_records(parsed, raw_output="")
    assert [r["name"] for r in records] == ["a", "b"]


def test_extract_sandbox_records_accepts_top_level_list() -> None:
    parsed = [{"name": "a"}, {"name": "b"}]
    records = _extract_sandbox_records(parsed, raw_output="")
    assert [r["name"] for r in records] == ["a", "b"]


def test_extract_sandbox_records_falls_back_to_jsonl() -> None:
    raw = '{"name": "a"}\n{"name": "b"}\n'
    records = _extract_sandbox_records(parsed="not-a-list-or-dict", raw_output=raw)
    assert [r["name"] for r in records] == ["a", "b"]
