"""Per-agent request headers stamped onto every forwarded request.

A host application that embeds the proxy may need every request reaching an
agent's backend to carry headers of its own choosing -- the same ones for
every agent, or a different set per agent. It writes those into a JSON file
and passes the path as ``--request-headers-file``; the proxy re-reads the file
whenever it changes and applies it to every proxied HTTP request and
WebSocket handshake. The proxy itself attaches no meaning to the headers.

The file is one JSON object: each key is an agent id (``agent-<hex>``) or
``"*"``, and each value maps header names to string values::

    {
      "*": {"X-Example-Requester": "owner"},
      "agent-<hex>": {"X-Example-Requester": "owner:alice"}
    }

A request to an agent gets that agent's own entry, else the ``"*"`` entry,
else nothing. Before anything is set, every header named anywhere in the file
(the union over all entries) is deleted from the inbound request, so a page
served by one agent can never smuggle a header another agent's entry would
have set.
"""

import json
import os
import re
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError
from pydantic import field_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.errors import ForwardRequestHeadersError

# The key whose headers apply to every agent without an entry of its own.
DEFAULT_AGENT_KEY: Final[str] = "*"

# An RFC 9110 field-name token: one or more tchar characters.
_HEADER_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

# Names the file may not set: request framing, the ones the proxy derives
# itself, and every hop-by-hop header (RFC 9110 section 7.6.1), whose values
# are meaningful only on one connection.
_RESERVED_HEADER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
)


def _validate_header_name(name: str) -> None:
    if _HEADER_NAME_RE.match(name) is None:
        raise ForwardRequestHeadersError(f"{name!r} is not a valid header name")
    if name.lower() in _RESERVED_HEADER_NAMES:
        raise ForwardRequestHeadersError(f"{name!r} is a framing or hop-by-hop header the proxy may not set")


class AgentRequestHeaders(FrozenModel):
    """What the proxy does to one request's headers before forwarding it: strip these names, then set these values."""

    names_to_strip: frozenset[str] = Field(
        default=frozenset(),
        description="Lowercased names deleted from the inbound request (every name the file mentions anywhere)",
    )
    values_by_name: dict[str, str] = Field(
        default_factory=dict, description="Headers set on the forwarded request, in the file's spelling"
    )


# The policy when no file is configured: nothing stripped, nothing set.
NO_REQUEST_HEADERS: Final[AgentRequestHeaders] = AgentRequestHeaders()


class RequestHeadersFile(FrozenModel):
    """The file's whole content: agent id (or ``"*"``) to the headers stamped on that agent's requests."""

    headers_by_agent_key: dict[str, dict[str, str]] = Field(default_factory=dict)

    @field_validator("headers_by_agent_key")
    @classmethod
    def _validate_entries(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for agent_key, headers in value.items():
            if agent_key != DEFAULT_AGENT_KEY:
                try:
                    AgentId(agent_key)
                except InvalidRandomIdError as exc:
                    raise ForwardRequestHeadersError(f"{agent_key!r} is neither an agent id nor {'*'!r}") from exc
            for name in headers:
                _validate_header_name(name)
        return value

    def headers_for_agent(self, agent_id: str) -> AgentRequestHeaders:
        """The agent's own entry, else the default entry, else nothing -- always with the file-wide strip set."""
        entry = self.headers_by_agent_key.get(agent_id)
        if entry is None:
            entry = self.headers_by_agent_key.get(DEFAULT_AGENT_KEY, {})
        return AgentRequestHeaders(names_to_strip=self.all_header_names, values_by_name=dict(entry))

    @property
    def all_header_names(self) -> frozenset[str]:
        """Every header name the file mentions, lowercased: the set stripped from every inbound request."""
        return frozenset(name.lower() for headers in self.headers_by_agent_key.values() for name in headers)


# The mtime and size of the file as last read; a change in either triggers a
# re-read on the next lookup.
_FileSignature = tuple[int, int]
# The signature a stat failure (other than a missing file) warns under, so the
# warning fires once until the file becomes statable (and changes) again.
_UNSTATABLE_SIGNATURE: _FileSignature = (-1, -1)


class RequestHeadersFileReader(MutableModel):
    """Serves per-agent request headers from a host-application-maintained JSON file, re-reading it when it changes.

    The file is stat'ed on every lookup (cheap: one syscall per proxied
    request) and re-parsed only when its mtime or size moved. A file that
    fails to parse is logged at warning once per distinct change and treated
    as empty until it parses again, so a half-written file degrades to
    forwarding requests untouched rather than to an error page.
    """

    path: Path = Field(frozen=True, description="The request-headers file the host application maintains")
    _file: RequestHeadersFile = PrivateAttr(default_factory=RequestHeadersFile)
    _loaded_signature: _FileSignature | None = PrivateAttr(default=None)
    _warned_signature: _FileSignature | None = PrivateAttr(default=None)

    def headers_for_agent(self, agent_id: str) -> AgentRequestHeaders:
        self._reload_if_changed()
        return self._file.headers_for_agent(agent_id)

    def _reload_if_changed(self) -> None:
        try:
            stat_result = os.stat(self.path)
        except FileNotFoundError:
            # The normal state for a host application with nothing to stamp yet.
            self._file = RequestHeadersFile()
            self._loaded_signature = None
            return
        except OSError as exc:
            self._file = RequestHeadersFile()
            self._loaded_signature = None
            self._warn_once(_UNSTATABLE_SIGNATURE, f"could not stat it: {exc}")
            return
        signature: _FileSignature = (stat_result.st_mtime_ns, stat_result.st_size)
        if signature == self._loaded_signature:
            return
        self._loaded_signature = signature
        self._file = self._parse(signature)

    def _parse(self, signature: _FileSignature) -> RequestHeadersFile:
        try:
            raw = self.path.read_text()
        except OSError as exc:
            self._warn_once(signature, f"could not read it: {exc}")
            return RequestHeadersFile()
        try:
            return RequestHeadersFile(headers_by_agent_key=json.loads(raw) if raw.strip() else {})
        except (ValueError, ValidationError) as exc:
            self._warn_once(signature, f"it is not an agent-id-to-headers JSON object: {exc}")
            return RequestHeadersFile()

    def _warn_once(self, signature: _FileSignature, reason: str) -> None:
        if self._warned_signature == signature:
            return
        self._warned_signature = signature
        logger.warning("Ignoring request-headers file {} until it changes again: {}", self.path, reason)
