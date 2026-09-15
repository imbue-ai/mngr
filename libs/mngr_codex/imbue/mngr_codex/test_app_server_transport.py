from contextlib import closing
from pathlib import Path

from imbue.mngr_codex.app_server_client import connect_app_server_transport


def test_large_history_frame_preserves_connection_for_followup_request(codex_large_frame_socket: Path) -> None:
    with closing(connect_app_server_transport(codex_large_frame_socket)) as transport:
        transport.send("history")
        assert transport.receive(5.0) == "x" * (2 * 1024 * 1024)
        transport.send("status")
        assert transport.receive(5.0) == "ready"
