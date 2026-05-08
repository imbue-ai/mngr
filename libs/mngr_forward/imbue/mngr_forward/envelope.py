"""JSONL envelope writer for the plugin's stdout stream.

Every line on stdout is a single JSON object with shape
``{"stream": "observe"|"event"|"forward", ["agent_id": ...,] "payload": ...}``.

A consumer (notably ``minds run``) parses these lines and dispatches based on
``stream``. ``observe`` and ``event`` lines carry raw JSON from the spawned
``mngr observe`` / ``mngr event`` subprocesses; ``forward`` lines carry the
plugin's own state events (``login_url``, ``listening``,
``reverse_tunnel_established``).
"""

import json
import sys
import threading
from typing import Any
from typing import IO

from loguru import logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.data_types import ListeningPayload
from imbue.mngr_forward.data_types import LoginUrlPayload
from imbue.mngr_forward.data_types import ReverseTunnelEstablishedPayload
from imbue.mngr_forward.primitives import ForwardPort


class EnvelopeWriter(MutableModel):
    """Serialize envelope lines to a single output stream under a lock.

    Lines are ``\\n``-terminated JSON. A single ``threading.Lock`` serializes
    writes so concurrent emitters (multiple subprocess reader threads + the
    forward-handler) cannot interleave bytes mid-line. ``flush()`` is called
    after each line so consumers see events promptly.

    The output stream is held as a PrivateAttr because pydantic cannot
    generate a schema for ``IO[str]``; the constructor accepts ``output=...``
    as a keyword argument and the default is ``sys.stdout``.
    """

    _output: IO[str] = PrivateAttr()
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __init__(self, output: IO[str] | None = None, **data: Any) -> None:
        super().__init__(**data)
        self._output = output if output is not None else sys.stdout

    @property
    def output(self) -> IO[str]:
        return self._output

    def emit_observe(self, line: str) -> None:
        """Forward one raw line of ``mngr observe`` stdout as an envelope.

        ``line`` is expected to already be a single JSON object on one line
        (i.e. one row of mngr's discovery JSONL stream). Empty / whitespace
        lines are dropped.
        """
        payload = self._parse_payload_line(line)
        if payload is None:
            return
        self._write_envelope({"stream": "observe", "payload": payload})

    def emit_event(self, agent_id: AgentId, line: str) -> None:
        """Forward one raw line of a per-agent ``mngr event`` stream as an envelope."""
        payload = self._parse_payload_line(line)
        if payload is None:
            return
        self._write_envelope({"stream": "event", "agent_id": str(agent_id), "payload": payload})

    def emit_login_url(self, url: str) -> None:
        """Emit the ``login_url`` plugin event."""
        self._write_envelope({"stream": "forward", "payload": LoginUrlPayload(url=url).model_dump(mode="json")})

    def emit_listening(self, host: str, port: ForwardPort) -> None:
        """Emit the ``listening`` plugin event."""
        self._write_envelope(
            {
                "stream": "forward",
                "payload": ListeningPayload(host=host, port=port).model_dump(mode="json"),
            }
        )

    def emit_reverse_tunnel_established(self, payload: ReverseTunnelEstablishedPayload) -> None:
        """Emit a ``reverse_tunnel_established`` plugin event."""
        self._write_envelope(
            {
                "stream": "forward",
                "agent_id": str(payload.agent_id),
                "payload": payload.model_dump(mode="json"),
            }
        )

    def close(self) -> None:
        """Flush the underlying stream. Does not close stdout."""
        with self._lock:
            try:
                self._output.flush()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _parse_payload_line(line: str) -> dict[str, Any] | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as e:
            # Pass non-JSON lines through as a string payload so consumers
            # can still see them, but log so the corruption is visible:
            # an upstream tool emitting prose to stdout is almost always a
            # real bug worth chasing.
            logger.warning("Non-JSON line on envelope stream ({}): {!r}", e, stripped[:200])
            return {"raw": stripped}
        if not isinstance(parsed, dict):
            return {"raw": stripped}
        return parsed

    def _write_envelope(self, envelope: dict[str, Any]) -> None:
        serialized = json.dumps(envelope, separators=(",", ":")) + "\n"
        with self._lock:
            self._output.write(serialized)
            try:
                self._output.flush()
            except (OSError, ValueError):
                pass
