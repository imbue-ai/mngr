import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from enum import auto
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from io import BufferedIOBase
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import pytest
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.agent_release_testing import MESSAGE_JOURNEY_BUSY_PROMPT
from imbue.mngr.agents.agent_release_testing import RECALL_PROMPT
from imbue.mngr.agents.agent_release_testing import SEED_TOOL_COMMAND
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.utils.env_utils import build_source_env_shell_commands
from imbue.mngr.utils.testing import write_executable_script
from imbue.mngr_grok.grok_sessions import SESSION_SUMMARY_FILENAME
from imbue.mngr_grok.plugin import GrokAgentConfig
from imbue.mngr_grok.resources.testing import TEST_FIXTURES_DIR

# The grok version CI installs and the binary-dependent tests and fixtures are built on.
GROK_VERSION_PIN_PATH: Final[Path] = TEST_FIXTURES_DIR / "grok_version.txt"
# Set to 1 where a test that needs the pinned grok must fail without it rather than skip.
REQUIRE_PINNED_GROK_ENV_VAR: Final[str] = "MNGR_GROK_REQUIRE_PINNED_BINARY"
_GROK_VERSION_PIN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_GROK_VERSION_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

PANE_FIXTURES_DIR: Final[Path] = TEST_FIXTURES_DIR / "panes"

_GROK_STUB_MODEL_ID: Final[str] = "stub-model"
# The alias of the stub's `[model.<alias>]` table, which `[models] default` selects.
_GROK_STUB_MODEL_ALIAS: Final[str] = "stub"
_GROK_STUB_API_KEY: Final[str] = "stub-key"
_GROK_STUB_CONTEXT_WINDOW: Final[int] = 131072
_GROK_STUB_ACCOUNT_EMAIL: Final[str] = "stub-user@test.invalid"
_GROK_STUB_SUBSCRIBER_TIER: Final[str] = "SuperGrokPro"
_GROK_STUB_SUBSCRIBER_TIER_DISPLAY: Final[str] = "SuperGrok Pro"
_GROK_STUB_USER_ID: Final[str] = "stub-user"
# The model ids `/v1/models` lists; a session signed in through the auth provider runs on `grok-4.6`.
_GROK_STUB_LISTED_MODEL_IDS: Final[tuple[str, ...]] = ("grok-4.6", "grok-build", _GROK_STUB_MODEL_ID)
# Grok renders at most 12 rows of 76 columns and rejects a bare URL in a consent body.
_GROK_STUB_CONSENT_BODY: Final[str] = "ZZBODY stub consent notice. Read the [Terms](https://x.ai/legal/tos)."
_XAI_AUTH_ISSUER: Final[str] = "https://auth.x.ai"
_GROK_STUB_ACCESS_TOKEN: Final[str] = "stub-token"
_GROK_STUB_ACCESS_TOKEN_LIFETIME_SECONDS: Final[int] = 3600

# Trigger words: a user prompt containing one makes the stub script its turn.
TOOL_CALL_TRIGGER: Final[str] = "TOOLTEST"
SLOW_TOOL_TRIGGER: Final[str] = "SLOWTOOL"
BACKGROUND_SHELL_TRIGGER: Final[str] = "BGTEST"
FOREGROUND_SUBAGENT_TRIGGER: Final[str] = "SUBAGENTFG"
BACKGROUND_SUBAGENT_TRIGGER: Final[str] = "SUBAGENTBG"
SLOW_REPLY_TRIGGER: Final[str] = "SLOWTEST"
HELD_REPLY_TRIGGER: Final[str] = "HOLDTEST"
EMPTY_REPLY_TRIGGER: Final[str] = "EMPTYREPLY"
SUBAGENT_PROMPT: Final[str] = "say hi from the subagent"
# What the TOOL_CALL_TRIGGER command writes in the work dir.
TOOL_CALL_MARKER_FILENAME: Final[str] = "tool_ran.txt"
TOOL_CALL_MARKER_TEXT: Final[str] = "stub-tool-ran"
TOOL_CALL_COMMAND: Final[str] = f"echo {TOOL_CALL_MARKER_TEXT} > {TOOL_CALL_MARKER_FILENAME} && echo done"

_SECRET_RE: Final[re.Pattern[str]] = re.compile(r"SECRET-[0-9A-Za-z]+")
_BACKGROUND_TASK_WAKE_RE: Final[re.Pattern[str]] = re.compile(r'Background task "[^"]+" completed')
_BACKGROUND_SUBAGENT_WAKE_RE: Final[re.Pattern[str]] = re.compile(
    r"While you were idle, [0-9]+ background subagents? completed"
)
_SYSTEM_REMINDER_TAG: Final[str] = "<system-reminder>"
_USER_QUERY_TAG: Final[str] = "<user_query>"
_SHELL_TOOL_NAME: Final[str] = "run_terminal_command"
_SPAWN_SUBAGENT_TOOL_NAME: Final[str] = "spawn_subagent"

# The k-th chat request reports 2**k prompt tokens up to this k, so a sum of prompt tokens names its requests.
_MAX_DOUBLING_CHAT_REQUEST_INDEX: Final[int] = 15
_PROMPT_TOKENS_BASE_AFTER_DOUBLING: Final[int] = 40000
_SLOW_CHUNK_INTERVAL_SECONDS: Final[float] = 1.0
_SERVE_THREAD_JOIN_TIMEOUT_SECONDS: Final[float] = 5.0
_REDACTED_HEADER_NAMES: Final[tuple[str, ...]] = ("authorization", "x-api-key")
_REDACTED_HEADER_PREFIX_LENGTH: Final[int] = 16
_GROK_REQUEST_ID_HEADER: Final[str] = "x-grok-req-id"


class GrokTestingError(PluginMngrError):
    """Base class for errors raised by the grok test helpers."""


class MalformedGrokVersionPinError(GrokTestingError, ValueError):
    """Raised when the grok version pin file does not hold a single X.Y.Z version."""


class GrokStubServerStateError(GrokTestingError, RuntimeError):
    """Raised when the stub model server is used before it is started or started twice."""


class GrokStubEgressAttemptError(GrokTestingError, AssertionError):
    """Raised when grok tried to reach another host through the stub, which refuses every proxied request."""


class GrokStubHandlerError(GrokTestingError, AssertionError):
    """Raised when the stub model server failed to answer a request grok sent it."""


class EmptyStubReplyError(GrokTestingError, RuntimeError):
    """Raised when the stub would send an empty assistant message that no scenario asked for."""


class GrokStubReplyKind(UpperCaseStrEnum):
    """How the stub answered a chat-completions request."""

    SIDE_CALL = auto()
    TOOL_CALL = auto()
    AFTER_TOOL = auto()
    WAKE = auto()
    CHILD = auto()
    RECALL = auto()
    SLOW = auto()
    HELD = auto()
    EMPTY = auto()
    REPLY = auto()


class GrokStubConsentNotice(FrozenModel):
    """A consent notice `/v1/settings` serves, which grok shows before the session starts."""

    notice_id: str = Field(description="The notice id, at most 64 bytes of [A-Za-z0-9._-]")
    version: PositiveInt = Field(description="The notice version, 1 to 10000; a higher one re-shows the notice")
    body: str = Field(default=_GROK_STUB_CONSENT_BODY, description="The notice text")


class GrokStubScenario(FrozenModel):
    """The account state and scripted failures the stub serves; a test may swap it while the server runs."""

    consent_notice: GrokStubConsentNotice | None = Field(
        default=None, description="The consent notice `/v1/settings` serves, if any"
    )
    empty_prompt_reply_attempt_count: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="How many attempts of a prompt containing EMPTY_REPLY_TRIGGER get an empty reply before a real one",
    )
    is_child_reply_slow: bool = Field(default=False, description="Whether subagent turns stream their reply slowly")


class GrokStubServerConfig(FrozenModel):
    """Pacing and logging of the stub model server."""

    request_log_path: Path | None = Field(
        default=None, description="A JSONL file that also receives every request record"
    )
    chunk_interval_seconds: PositiveFloat = Field(
        default=PositiveFloat(0.05), description="The pause between streamed chunks of an ordinary reply"
    )
    slow_reply_seconds: PositiveFloat = Field(
        default=PositiveFloat(12.0), description="How long a SLOW_REPLY_TRIGGER reply (and a slow child) streams"
    )
    held_reply_seconds: PositiveFloat = Field(
        default=PositiveFloat(30.0),
        description="How long a HELD_REPLY_TRIGGER reply sends nothing at all before it answers like an ordinary one",
    )
    busy_prompt_reply_seconds: PositiveFloat = Field(
        default=PositiveFloat(45.0),
        description="How long the message journey's busy prompt streams; it outlasts TUI_READY_TIMEOUT_SECONDS",
    )
    seed_reply_seconds: PositiveFloat = Field(
        default=PositiveFloat(10.0),
        description="How long the reply after the seed command streams, so the turn outlasts the send's confirmation",
    )
    slow_tool_seconds: PositiveInt = Field(
        default=PositiveInt(8), description="How long the SLOW_TOOL_TRIGGER foreground command sleeps"
    )
    background_shell_seconds: PositiveInt = Field(
        default=PositiveInt(8), description="How long the BACKGROUND_SHELL_TRIGGER background command sleeps"
    )
    tolerated_proxy_targets: frozenset[str] = Field(
        default=frozenset(), description="Proxy targets (`host:port`) the egress check accepts"
    )


class GrokStubUsage(FrozenModel):
    """The token usage the stub reports for one chat request."""

    prompt_tokens: NonNegativeInt = Field(description="Reported prompt tokens")
    completion_tokens: NonNegativeInt = Field(description="Reported completion tokens")
    total_tokens: NonNegativeInt = Field(description="Reported total tokens")


class GrokStubToolCall(FrozenModel):
    """A tool call the stub makes the model emit."""

    tool_name: str = Field(description="The grok tool the call invokes")
    arguments: dict[str, Any] = Field(description="The call's JSON arguments")


class GrokStubChatReply(FrozenModel):
    """How the stub answered one chat-completions request."""

    chat_request_index: PositiveInt = Field(description="1 for the server's first chat request, and so on")
    kind: GrokStubReplyKind = Field(description="Which scripted behaviour answered")
    model: str = Field(description="The model id the request named")
    text: str = Field(description="The assistant text; empty for a tool call and for an EMPTY reply")
    tool_call: GrokStubToolCall | None = Field(description="The tool call, for a TOOL_CALL reply")
    usage: GrokStubUsage = Field(description="The usage the reply reports")
    chunk_interval_seconds: PositiveFloat = Field(description="The pause between streamed chunks")


class GrokStubRequestRecord(FrozenModel):
    """One request the stub server received, and the chat reply it gave, if any."""

    request_index: PositiveInt = Field(description="1 for the server's first request, and so on")
    received_at: datetime = Field(description="When the request arrived")
    method: str = Field(description="The HTTP method")
    path: str = Field(description="The request target, query included")
    headers: dict[str, str] = Field(description="The request headers, credentials redacted")
    body: Any = Field(description="The parsed JSON body, or None")
    raw_body_byte_count: NonNegativeInt = Field(description="The body's length in bytes")
    is_proxy_attempt: bool = Field(description="Whether the request tried to use the stub as a proxy (refused)")
    response_status: int = Field(description="The HTTP status the stub answered with")
    chat_reply: GrokStubChatReply | None = Field(description="The reply, for a chat-completions request")


@pure
def build_env_file_prefix(host_env_path: Path, agent_env_path: Path) -> str:
    """The shell prefix `host.build_source_env_prefix(agent)` builds for these host and agent env files."""
    return " && ".join(build_source_env_shell_commands(host_env_path, agent_env_path)) + " && "


def write_agent_env_prefix(directory: Path, variables: Mapping[str, str]) -> str:
    """Write an agent env file setting ``variables`` in ``directory``; return its prefix, with no host env file."""
    agent_env_path = directory / f"agent_env_{uuid4().hex}"
    agent_env_path.write_text("".join(f"{name}={shlex.quote(value)}\n" for name, value in variables.items()))
    return build_env_file_prefix(directory / "absent_host_env", agent_env_path)


def read_pane_fixture(file_name: str) -> str:
    """The text of a captured grok pane in PANE_FIXTURES_DIR."""
    return (PANE_FIXTURES_DIR / file_name).read_text(encoding="utf-8")


def make_grok_session_dir(sessions_dir: Path, bucket: str, session_id: str, is_resumable: bool = True) -> Path:
    """Create `<sessions_dir>/<bucket>/<session_id>` holding an updates.jsonl, and a summary.json when resumable."""
    session_dir = sessions_dir / bucket / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "updates.jsonl").write_text('{"method": "session/update"}\n')
    if is_resumable:
        (session_dir / SESSION_SUMMARY_FILENAME).write_text("{}\n")
    return session_dir


def put_executable_first_on_path(bin_dir: Path, monkeypatch: pytest.MonkeyPatch, name: str, script: str) -> Path:
    """Write `script` as the executable `<bin_dir>/<name>` and put `bin_dir` first on PATH; returns the executable."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable_path = bin_dir / name
    write_executable_script(executable_path, script)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return executable_path


@pure
def select_always_approve_option(permission_card_pane: str) -> str:
    """A captured permission card with its selection moved from `Yes, proceed` to the always-approve option."""
    return permission_card_pane.replace("(○) Yes, and don't ask again", "(●) Yes, and don't ask again").replace(
        "(●) Yes, proceed", "(○) Yes, proceed"
    )


def read_pinned_grok_version() -> str:
    """The grok version in GROK_VERSION_PIN_PATH, e.g. `1.0.62`."""
    version = GROK_VERSION_PIN_PATH.read_text().strip()
    if _GROK_VERSION_PIN_RE.fullmatch(version) is None:
        raise MalformedGrokVersionPinError(
            f"{GROK_VERSION_PIN_PATH} must hold one X.Y.Z grok version, found {version!r}"
        )
    return version


def is_pinned_grok_required() -> bool:
    """Whether a test that needs the pinned grok fails without it instead of skipping."""
    return os.environ.get(REQUIRE_PINNED_GROK_ENV_VAR) == "1"


def find_pinned_grok_mismatch(grok_path: str | None) -> str | None:
    """Why the `grok` at `grok_path` (the one first on PATH) is not the pinned grok, or None when it is."""
    pinned_version = read_pinned_grok_version()
    if grok_path is None:
        return (
            f"no grok on PATH; install the pinned grok {pinned_version} (GROK_VERSION_PIN_PATH) with "
            f"`env -u GROK_HOME npm install -g @xai-official/grok@{pinned_version}` or put a directory holding it "
            "first on PATH"
        )
    version_output = subprocess.run(
        [grok_path, "--version"],
        capture_output=True,
        text=True,
        timeout=_GROK_VERSION_COMMAND_TIMEOUT_SECONDS,
        check=False,
    ).stdout
    if not version_output.startswith(f"grok {pinned_version} ("):
        return f"{grok_path} reports {version_output.strip()!r}, not the pinned grok {pinned_version}"
    return None


@pure
def build_stub_custom_model_settings_overrides(api_base_url: str) -> dict[str, Any]:
    """A `settings_overrides` patch making the stub grok's default model, as the custom model `[model.stub]`."""
    return {
        "models": {"default": _GROK_STUB_MODEL_ALIAS},
        "model": {
            _GROK_STUB_MODEL_ALIAS: {
                "model": _GROK_STUB_MODEL_ID,
                "base_url": api_base_url,
                "api_key": _GROK_STUB_API_KEY,
                "name": "Stub",
                "context_window": _GROK_STUB_CONTEXT_WINDOW,
            }
        },
    }


@pure
def build_stub_auth_provider_environment(api_base_url: str, auth_provider_command_path: Path) -> dict[str, str]:
    """Signs grok in with the auth provider script's token, with every xAI endpoint on the stub."""
    return {
        "GROK_AUTH_PROVIDER_COMMAND": str(auth_provider_command_path),
        "GROK_CLI_CHAT_PROXY_BASE_URL": api_base_url,
        "GROK_XAI_API_BASE_URL": api_base_url,
        "GROK_MODELS_BASE_URL": api_base_url,
        "GROK_FEEDBACK_BASE_URL": api_base_url,
        "GROK_TRACE_UPLOAD_URL": api_base_url,
    }


@pure
def build_egress_tripwire_environment(origin_url: str) -> dict[str, str]:
    """Points the proxy variables and grok's other xAI origins at the stub, which logs and refuses them."""
    host_and_port = origin_url.removeprefix("http://")
    proxy_environment = {
        name: origin_url
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
    }
    return {
        **proxy_environment,
        "GROK_MANAGED_CONFIG_URL": f"{origin_url}/v1/deployment/config",
        "GROK_CODE_WEB_URL": origin_url,
        "GROK_CONVERSATIONS_BASE_URL": f"{origin_url}/v1",
        "GROK_WS_URL": f"ws://{host_and_port}/ws/code-agent",
        "GROK_WS_ORIGIN": origin_url,
        "GROK_OAUTH2_ISSUER": f"{origin_url}/oauth-tripwire",
        "GROK_OAUTH2_CLIENT_ID": "stub-tripwire-client",
    }


def write_stub_auth_provider_script(script_path: Path) -> None:
    """Write the executable GROK_AUTH_PROVIDER_COMMAND that prints a stub token issued by xAI."""
    token = {
        "access_token": _GROK_STUB_ACCESS_TOKEN,
        "expires_in": _GROK_STUB_ACCESS_TOKEN_LIFETIME_SECONDS,
        "issuer": _XAI_AUTH_ISSUER,
    }
    write_executable_script(script_path, f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(json.dumps(token))}\n")


@pure
def _build_user_response() -> dict[str, Any]:
    return {
        "userId": _GROK_STUB_USER_ID,
        "email": _GROK_STUB_ACCOUNT_EMAIL,
        "subscriptionTier": _GROK_STUB_SUBSCRIBER_TIER,
    }


@pure
def _build_settings_response(scenario: GrokStubScenario) -> dict[str, Any]:
    settings: dict[str, Any] = {"allow_access": True, "subscription_tier_display": _GROK_STUB_SUBSCRIBER_TIER_DISPLAY}
    if scenario.consent_notice is not None:
        settings["consent_gate"] = {
            "id": scenario.consent_notice.notice_id,
            "version": scenario.consent_notice.version,
            "body": scenario.consent_notice.body,
        }
    return settings


@pure
def _build_models_response() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": 1234567890,
                "owned_by": "test",
                "context_length": _GROK_STUB_CONTEXT_WINDOW,
            }
            for model_id in _GROK_STUB_LISTED_MODEL_IDS
        ],
    }


@pure
def get_chat_messages(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, Mapping)]


@pure
def extract_message_text(message: Mapping[str, Any]) -> str:
    """The text of a chat message whose content is a string or a list of text parts."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, Mapping))
    return ""


@pure
def find_last_user_text(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return extract_message_text(message)
    return ""


@pure
def _get_offered_tool_names(body: Mapping[str, Any]) -> tuple[str, ...]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return ()
    names: list[str] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str):
            names.append(name)
    return tuple(names)


@pure
def _is_auto_wake_reminder(user_text: str) -> bool:
    """Whether a user message is grok's auto-wake for a finished background shell or background subagents."""
    if _SYSTEM_REMINDER_TAG not in user_text or _USER_QUERY_TAG in user_text:
        return False
    return (
        _BACKGROUND_TASK_WAKE_RE.search(user_text) is not None
        or _BACKGROUND_SUBAGENT_WAKE_RE.search(user_text) is not None
    )


@pure
def _build_stub_usage(chat_request_index: int) -> GrokStubUsage:
    """Usage that differs for every request k: 2**k prompt tokens, then _PROMPT_TOKENS_BASE_AFTER_DOUBLING + k."""
    prompt_tokens = (
        2**chat_request_index
        if chat_request_index <= _MAX_DOUBLING_CHAT_REQUEST_INDEX
        else _PROMPT_TOKENS_BASE_AFTER_DOUBLING + chat_request_index
    )
    return GrokStubUsage(
        prompt_tokens=NonNegativeInt(prompt_tokens),
        completion_tokens=NonNegativeInt(chat_request_index),
        total_tokens=NonNegativeInt(prompt_tokens + chat_request_index),
    )


@pure
def _get_empty_reply_request_key(grok_request_id: str | None, body: Mapping[str, Any]) -> str:
    """The key under which attempts of one request are counted; grok resends a retried request with its request id."""
    return grok_request_id if grok_request_id else find_last_user_text(get_chat_messages(body))


@pure
def _find_secret_in_history(messages: Sequence[Mapping[str, Any]]) -> str | None:
    secrets = [match for message in messages[:-1] for match in _SECRET_RE.findall(extract_message_text(message))]
    return secrets[-1] if secrets else None


@pure
def _build_slow_text(label: str, duration_seconds: float) -> str:
    """`label` padded with filler words, so that streaming it at the slow pace takes about `duration_seconds`."""
    # n words stream as n chunks, a usage chunk and [DONE], with a pause between each.
    word_count = max(math.ceil(duration_seconds / _SLOW_CHUNK_INTERVAL_SECONDS) - 1, 1)
    label_words = label.split(" ")
    filler_words = [f"w{index}" for index in range(1, max(word_count - len(label_words), 0) + 1)]
    return " ".join([*label_words, *filler_words])


@pure
def _build_shell_tool_call(command: str, description: str, is_background: bool) -> GrokStubToolCall:
    arguments: dict[str, Any] = {"command": command, "description": description}
    if is_background:
        arguments["block_until_ms"] = 0
    return GrokStubToolCall(tool_name=_SHELL_TOOL_NAME, arguments=arguments)


@pure
def _build_spawn_subagent_tool_call(is_background: bool) -> GrokStubToolCall:
    return GrokStubToolCall(
        tool_name=_SPAWN_SUBAGENT_TOOL_NAME,
        arguments={
            "prompt": SUBAGENT_PROMPT,
            "description": "stub subagent bg" if is_background else "stub subagent fg",
            "background": is_background,
        },
    )


@pure
def _find_triggered_tool_call(
    user_text: str, offered_tool_names: Sequence[str], config: GrokStubServerConfig
) -> GrokStubToolCall | None:
    """The tool call a prompt's trigger word asks for, when grok offers that tool; the first trigger listed wins."""
    shell_calls = (
        (
            BACKGROUND_SHELL_TRIGGER,
            _build_shell_tool_call(
                f"sleep {config.background_shell_seconds}; echo bg-done > bg_done.txt", "background sleep", True
            ),
        ),
        (TOOL_CALL_TRIGGER, _build_shell_tool_call(TOOL_CALL_COMMAND, "write marker file", False)),
        (
            SLOW_TOOL_TRIGGER,
            _build_shell_tool_call(
                f"sleep {config.slow_tool_seconds}; echo slowtool-done", "slow foreground sleep", False
            ),
        ),
        (SEED_TOOL_COMMAND, _build_shell_tool_call(SEED_TOOL_COMMAND, "echo the seed marker", False)),
    )
    subagent_calls = (
        (BACKGROUND_SUBAGENT_TRIGGER, _build_spawn_subagent_tool_call(True)),
        (FOREGROUND_SUBAGENT_TRIGGER, _build_spawn_subagent_tool_call(False)),
    )
    offered_calls = (shell_calls if _SHELL_TOOL_NAME in offered_tool_names else ()) + (
        subagent_calls if _SPAWN_SUBAGENT_TOOL_NAME in offered_tool_names else ()
    )
    for trigger_word, tool_call in offered_calls:
        if trigger_word in user_text:
            return tool_call
    return None


class _StubTurnDecision(FrozenModel):
    """The scripted answer to a chat request, before usage and pacing are attached."""

    kind: GrokStubReplyKind = Field(description="Which scripted behaviour answers")
    text: str = Field(description="The assistant text")
    tool_call: GrokStubToolCall | None = Field(default=None, description="The tool call, if any")
    slow_duration_seconds: float | None = Field(default=None, description="How long a slow reply streams")


@pure
def _decide_after_tool_reply(
    messages: Sequence[Mapping[str, Any]], config: GrokStubServerConfig, chat_request_index: int
) -> _StubTurnDecision:
    if SEED_TOOL_COMMAND in find_last_user_text(messages):
        return _StubTurnDecision(
            kind=GrokStubReplyKind.SLOW,
            text=_build_slow_text(f"STUB_SEED_REPLY k={chat_request_index}: ACK", config.seed_reply_seconds),
            slow_duration_seconds=config.seed_reply_seconds,
        )
    return _StubTurnDecision(
        kind=GrokStubReplyKind.AFTER_TOOL, text=f"STUB_REPLY k={chat_request_index}: tool finished"
    )


@pure
def _decide_user_turn_reply(
    *,
    messages: Sequence[Mapping[str, Any]],
    offered_tool_names: Sequence[str],
    scenario: GrokStubScenario,
    config: GrokStubServerConfig,
    chat_request_index: int,
    prior_empty_reply_count: int,
) -> _StubTurnDecision:
    user_text = find_last_user_text(messages)
    tool_call = _find_triggered_tool_call(user_text, offered_tool_names, config)
    if _is_auto_wake_reminder(user_text):
        return _StubTurnDecision(
            kind=GrokStubReplyKind.WAKE,
            text=f"STUB_WAKE_REPLY k={chat_request_index}: acknowledged background completion",
        )
    elif EMPTY_REPLY_TRIGGER in user_text and prior_empty_reply_count < scenario.empty_prompt_reply_attempt_count:
        return _StubTurnDecision(kind=GrokStubReplyKind.EMPTY, text="")
    elif SUBAGENT_PROMPT in user_text and scenario.is_child_reply_slow:
        return _StubTurnDecision(
            kind=GrokStubReplyKind.CHILD,
            text=_build_slow_text(f"STUB_CHILD_REPLY k={chat_request_index}:", config.slow_reply_seconds),
            slow_duration_seconds=config.slow_reply_seconds,
        )
    elif SUBAGENT_PROMPT in user_text:
        return _StubTurnDecision(
            kind=GrokStubReplyKind.CHILD, text=f"STUB_CHILD_REPLY k={chat_request_index}: hi from the subagent"
        )
    elif tool_call is not None:
        return _StubTurnDecision(kind=GrokStubReplyKind.TOOL_CALL, text="", tool_call=tool_call)
    elif RECALL_PROMPT in user_text:
        secret = _find_secret_in_history(messages)
        return _StubTurnDecision(
            kind=GrokStubReplyKind.RECALL,
            text=f"STUB_RECALL: {secret}" if secret is not None else "STUB_RECALL: no secret in the history",
        )
    elif HELD_REPLY_TRIGGER in user_text:
        return _StubTurnDecision(kind=GrokStubReplyKind.HELD, text=f"STUB_HELD_REPLY k={chat_request_index}: OK")
    elif SLOW_REPLY_TRIGGER in user_text or MESSAGE_JOURNEY_BUSY_PROMPT in user_text:
        duration_seconds = (
            config.slow_reply_seconds if SLOW_REPLY_TRIGGER in user_text else config.busy_prompt_reply_seconds
        )
        return _StubTurnDecision(
            kind=GrokStubReplyKind.SLOW,
            text=_build_slow_text(f"STUB_SLOW_REPLY k={chat_request_index}:", duration_seconds),
            slow_duration_seconds=duration_seconds,
        )
    else:
        return _StubTurnDecision(kind=GrokStubReplyKind.REPLY, text=f"STUB_REPLY k={chat_request_index}: OK")


@pure
def _plan_chat_reply(
    *,
    body: Mapping[str, Any],
    scenario: GrokStubScenario,
    config: GrokStubServerConfig,
    chat_request_index: int,
    # Empty replies already given to earlier attempts of this request (see _get_empty_reply_request_key).
    prior_empty_reply_count: int,
) -> GrokStubChatReply:
    """Decide how the stub answers one chat-completions request; only a scenario count yields an empty reply."""
    messages = get_chat_messages(body)
    offered_tool_names = _get_offered_tool_names(body)
    last_role = messages[-1].get("role") if messages else None
    if _SHELL_TOOL_NAME not in offered_tool_names:
        decision = _StubTurnDecision(kind=GrokStubReplyKind.SIDE_CALL, text=f"STUB_SIDE_REPLY k={chat_request_index}")
    elif last_role == "tool":
        decision = _decide_after_tool_reply(messages, config, chat_request_index)
    elif last_role == "user":
        decision = _decide_user_turn_reply(
            messages=messages,
            offered_tool_names=offered_tool_names,
            scenario=scenario,
            config=config,
            chat_request_index=chat_request_index,
            prior_empty_reply_count=prior_empty_reply_count,
        )
    else:
        decision = _StubTurnDecision(kind=GrokStubReplyKind.REPLY, text=f"STUB_REPLY k={chat_request_index}: OK")
    if decision.kind not in (GrokStubReplyKind.EMPTY, GrokStubReplyKind.TOOL_CALL) and not decision.text.strip():
        raise EmptyStubReplyError(f"The stub planned an empty {decision.kind} reply, which makes grok retry")
    return GrokStubChatReply(
        chat_request_index=PositiveInt(chat_request_index),
        kind=decision.kind,
        model=str(body.get("model") or _GROK_STUB_MODEL_ID),
        text=decision.text,
        tool_call=decision.tool_call,
        usage=_build_stub_usage(chat_request_index),
        chunk_interval_seconds=PositiveFloat(
            config.chunk_interval_seconds if decision.slow_duration_seconds is None else _SLOW_CHUNK_INTERVAL_SECONDS
        ),
    )


@pure
def _build_chunk(reply: GrokStubChatReply, choices: Sequence[Mapping[str, Any]], is_usage_attached: bool) -> str:
    chunk: dict[str, Any] = {
        "id": f"chatcmpl-stub-{reply.chat_request_index}",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": reply.model,
        "choices": list(choices),
    }
    if is_usage_attached:
        chunk["usage"] = reply.usage.model_dump()
    return json.dumps(chunk)


@pure
def _build_wire_tool_calls(reply: GrokStubChatReply, tool_call: GrokStubToolCall) -> list[dict[str, Any]]:
    return [
        {
            "index": 0,
            "id": f"call_stub_{reply.chat_request_index}",
            "type": "function",
            "function": {"name": tool_call.tool_name, "arguments": json.dumps(tool_call.arguments)},
        }
    ]


@pure
def _build_chat_completion_sse_events(reply: GrokStubChatReply) -> list[str]:
    """The `data:` payloads of a streamed reply, in the chat-completions chunk format grok reads."""
    if reply.tool_call is not None:
        delta = {"role": "assistant", "content": None, "tool_calls": _build_wire_tool_calls(reply, reply.tool_call)}
        events = [
            _build_chunk(reply, [{"index": 0, "delta": delta, "finish_reason": None}], False),
            _build_chunk(reply, [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], True),
        ]
    elif reply.text == "":
        events = [
            _build_chunk(reply, [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": "stop"}], False),
            _build_chunk(reply, [], True),
        ]
    else:
        words = reply.text.split(" ")
        word_chunks = [
            _build_chunk(
                reply,
                [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": word} if idx == 0 else {"content": f" {word}"},
                        "finish_reason": "stop" if idx == len(words) - 1 else None,
                    }
                ],
                False,
            )
            for idx, word in enumerate(words)
        ]
        events = [*word_chunks, _build_chunk(reply, [], True)]
    return [*events, "[DONE]"]


@pure
def _build_chat_completion_json(reply: GrokStubChatReply) -> dict[str, Any]:
    """A non-streamed reply in the chat-completions format."""
    message: dict[str, Any] = {"role": "assistant", "content": reply.text if reply.tool_call is None else None}
    if reply.tool_call is not None:
        message["tool_calls"] = _build_wire_tool_calls(reply, reply.tool_call)
    return {
        "id": f"chatcmpl-stub-{reply.chat_request_index}",
        "object": "chat.completion",
        "created": 1,
        "model": reply.model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "stop" if reply.tool_call is None else "tool_calls"}
        ],
        "usage": reply.usage.model_dump(),
    }


@pure
def _redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: f"{value[:_REDACTED_HEADER_PREFIX_LENGTH]}...(redacted)"
        if name.lower() in _REDACTED_HEADER_NAMES
        else value
        for name, value in headers.items()
    }


def _read_raw_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    content_length = handler.headers.get("Content-Length", "")
    byte_count = int(content_length) if content_length.isdecimal() else 0
    return handler.rfile.read(byte_count) if byte_count > 0 else b""


def _parse_json_request_body(handler: BaseHTTPRequestHandler, raw_body: bytes) -> Any:
    if not raw_body or handler.headers.get("Content-Encoding"):
        return None
    try:
        return json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("The grok stub received a body that is not JSON on {}: {}", handler.path, exc)
        return None


def _send_json_response(handler: BaseHTTPRequestHandler, status: int, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(encoded)


def _write_and_flush(stream: BufferedIOBase, data: bytes) -> None:
    stream.write(data)
    stream.flush()


class _GrokStubRequestHandler(BaseHTTPRequestHandler):
    """Hands each request to the stub model server bound to this handler class."""

    protocol_version = "HTTP/1.1"
    stub_model_server: "GrokStubModelServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("A grok stub client dropped its connection")

    def do_GET(self) -> None:
        self.stub_model_server.handle_request(self)

    def do_POST(self) -> None:
        self.stub_model_server.handle_request(self)

    def do_CONNECT(self) -> None:
        self.stub_model_server.handle_request(self)

    def do_PUT(self) -> None:
        self.stub_model_server.handle_request(self)

    def do_PATCH(self) -> None:
        self.stub_model_server.handle_request(self)

    def do_DELETE(self) -> None:
        self.stub_model_server.handle_request(self)

    def do_HEAD(self) -> None:
        self.stub_model_server.handle_request(self)

    def do_OPTIONS(self) -> None:
        self.stub_model_server.handle_request(self)


def _bind_request_handler_class(stub_model_server: "GrokStubModelServer") -> type[_GrokStubRequestHandler]:
    return type("_BoundGrokStubRequestHandler", (_GrokStubRequestHandler,), {"stub_model_server": stub_model_server})


class _GrokStubHttpServer(ThreadingHTTPServer):
    """The stub's listener on 127.0.0.1; its request threads never hold the interpreter open."""

    daemon_threads = True

    def __init__(
        self,
        handler_class: type[_GrokStubRequestHandler],
        record_handler_error: Callable[[BaseException], None],
    ) -> None:
        super().__init__(("127.0.0.1", 0), handler_class)
        self._record_handler_error = record_handler_error

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exception()
        logger.opt(exception=error).error("The grok stub failed to answer a request from {}", client_address)
        if error is not None:
            self._record_handler_error(error)


class GrokStubModelServer(MutableModel):
    """A local stand-in for grok's model endpoint and xAI's account API, which records every request."""

    config: GrokStubServerConfig = Field(frozen=True, description="Pacing and logging")
    scenario: GrokStubScenario = Field(default_factory=GrokStubScenario, description="The served account state")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _http_server: _GrokStubHttpServer | None = PrivateAttr(default=None)
    _serve_thread: threading.Thread | None = PrivateAttr(default=None)
    _records: list[GrokStubRequestRecord] = PrivateAttr(default_factory=list)
    _chat_request_count: int = PrivateAttr(default=0)
    _empty_reply_count_by_request_key: dict[str, int] = PrivateAttr(default_factory=dict)
    _handler_errors: list[BaseException] = PrivateAttr(default_factory=list)

    def start(self) -> None:
        """Listen on a free loopback port and serve from a background thread."""
        if self._http_server is not None:
            raise GrokStubServerStateError("The grok stub model server is already running")
        self._stop_event.clear()
        http_server = _GrokStubHttpServer(_bind_request_handler_class(self), self._record_handler_error)
        serve_thread = threading.Thread(target=http_server.serve_forever, name="grok-stub-model-server", daemon=True)
        serve_thread.start()
        self._http_server = http_server
        self._serve_thread = serve_thread

    def stop(self) -> None:
        """Cut in-flight streams and stop listening; a no-op when the server is not running."""
        http_server = self._http_server
        if http_server is None:
            return
        self._stop_event.set()
        http_server.shutdown()
        http_server.server_close()
        if self._serve_thread is not None:
            self._serve_thread.join(timeout=_SERVE_THREAD_JOIN_TIMEOUT_SECONDS)
        self._http_server = None
        self._serve_thread = None

    def get_origin_url(self) -> str:
        """The server's origin, e.g. `http://127.0.0.1:53817`."""
        if self._http_server is None:
            raise GrokStubServerStateError("The grok stub model server is not running")
        return f"http://127.0.0.1:{self._http_server.server_port}"

    def get_api_base_url(self) -> str:
        """The stub's API base URL, e.g. `http://127.0.0.1:53817/v1`."""
        return f"{self.get_origin_url()}/v1"

    def set_scenario(self, scenario: GrokStubScenario) -> None:
        with self._lock:
            self.scenario = scenario

    def get_request_records(self) -> tuple[GrokStubRequestRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def _record_handler_error(self, error: BaseException) -> None:
        with self._lock:
            self._handler_errors.append(error)

    def check_no_egress_attempts_or_handler_errors(self) -> None:
        """Raise when the stub failed to answer a request, or grok used it as a proxy to an untolerated target."""
        with self._lock:
            handler_errors = tuple(self._handler_errors)
        if handler_errors:
            raise GrokStubHandlerError(
                f"The grok stub failed to answer {len(handler_errors)} request(s): {handler_errors!r}"
            ) from handler_errors[0]
        proxy_attempt_targets = [
            record.path
            for record in self.get_request_records()
            if record.is_proxy_attempt and record.path not in self.config.tolerated_proxy_targets
        ]
        if proxy_attempt_targets:
            raise GrokStubEgressAttemptError(
                f"grok tried to reach the network through the stub's proxy: {proxy_attempt_targets}"
            )

    def handle_request(self, handler: _GrokStubRequestHandler) -> None:
        """Answer one HTTP request and record it."""
        raw_body = _read_raw_request_body(handler)
        body = _parse_json_request_body(handler, raw_body)
        route = handler.path.split("?", 1)[0]
        with self._lock:
            scenario = self.scenario
        if handler.command == "CONNECT" or handler.path.startswith(("http://", "https://")):
            self._record_request(handler, raw_body, body, response_status=403, is_proxy_attempt=True, chat_reply=None)
            _send_json_response(handler, 403, {"error": "the grok stub refuses all egress"})
        elif handler.command == "POST" and route.endswith("/chat/completions"):
            self._answer_chat_completion(handler, raw_body, body if isinstance(body, dict) else {})
        else:
            status, payload = _route_account_api_request(handler.command, route, body, scenario)
            self._record_request(
                handler, raw_body, body, response_status=status, is_proxy_attempt=False, chat_reply=None
            )
            _send_json_response(handler, status, payload)

    def _record_request(
        self,
        handler: _GrokStubRequestHandler,
        raw_body: bytes,
        body: Any,
        *,
        response_status: int,
        is_proxy_attempt: bool,
        chat_reply: GrokStubChatReply | None,
    ) -> None:
        with self._lock:
            record = GrokStubRequestRecord(
                request_index=PositiveInt(len(self._records) + 1),
                received_at=datetime.now(timezone.utc),
                method=handler.command,
                path=handler.path,
                headers=_redact_headers(dict(handler.headers.items())),
                body=body,
                raw_body_byte_count=NonNegativeInt(len(raw_body)),
                is_proxy_attempt=is_proxy_attempt,
                response_status=response_status,
                chat_reply=chat_reply,
            )
            self._records.append(record)
            if self.config.request_log_path is not None:
                with self.config.request_log_path.open("a") as log_file:
                    log_file.write(record.model_dump_json() + "\n")

    def _claim_chat_request(self, request_key: str) -> tuple[int, int, GrokStubScenario]:
        with self._lock:
            self._chat_request_count += 1
            return (
                self._chat_request_count,
                self._empty_reply_count_by_request_key.get(request_key, 0),
                self.scenario,
            )

    def _answer_chat_completion(
        self, handler: _GrokStubRequestHandler, raw_body: bytes, body: Mapping[str, Any]
    ) -> None:
        request_key = _get_empty_reply_request_key(handler.headers.get(_GROK_REQUEST_ID_HEADER), body)
        chat_request_index, prior_empty_reply_count, scenario = self._claim_chat_request(request_key)
        reply = _plan_chat_reply(
            body=body,
            scenario=scenario,
            config=self.config,
            chat_request_index=chat_request_index,
            prior_empty_reply_count=prior_empty_reply_count,
        )
        if reply.kind == GrokStubReplyKind.EMPTY:
            with self._lock:
                self._empty_reply_count_by_request_key[request_key] = prior_empty_reply_count + 1
        self._record_request(handler, raw_body, body, response_status=200, is_proxy_attempt=False, chat_reply=reply)
        if reply.kind == GrokStubReplyKind.HELD and self._stop_event.wait(self.config.held_reply_seconds):
            return
        if body.get("stream", True) is False:
            _send_json_response(handler, 200, _build_chat_completion_json(reply))
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        self._stream_sse_events(handler, _build_chat_completion_sse_events(reply), reply.chunk_interval_seconds)

    def _stream_sse_events(
        self, handler: _GrokStubRequestHandler, events: Sequence[str], chunk_interval_seconds: float
    ) -> None:
        for idx, event in enumerate(events):
            try:
                _write_and_flush(handler.wfile, f"data: {event}\n\n".encode())
            except (BrokenPipeError, ConnectionResetError):
                logger.debug("The grok stub's client closed the stream of {} early", handler.path)
                return
            is_last_event = idx == len(events) - 1
            if not is_last_event and self._stop_event.wait(chunk_interval_seconds):
                return


@pure
def _route_account_api_request(
    method: str, route: str, body: Any, scenario: GrokStubScenario
) -> tuple[int, dict[str, Any]]:
    """The status and JSON payload for every request that is not a chat completion or a proxy attempt."""
    if method == "POST" and route.endswith(("/consent/accept", "/privacy/coding-data-retention")):
        return 200, dict(body) if isinstance(body, Mapping) else {}
    elif method == "GET" and route.endswith("/models"):
        return 200, _build_models_response()
    elif method == "GET" and route.endswith("/user"):
        return 200, _build_user_response()
    elif method == "GET" and route.endswith("/settings"):
        return 200, _build_settings_response(scenario)
    else:
        return 404, {"error": "not found (grok stub)"}


@contextmanager
def running_grok_stub_model_server(config: GrokStubServerConfig) -> Generator[GrokStubModelServer, None, None]:
    """Run a stub model server for the block, then fail if it failed a request or grok reached past it."""
    server = GrokStubModelServer(config=config)
    server.start()
    try:
        yield server
    finally:
        server.stop()
    server.check_no_egress_attempts_or_handler_errors()


def build_stub_grok_config(stub: GrokStubModelServer, environment: Mapping[str, str]) -> GrokAgentConfig:
    """A grok agent type on the stub model: always-approve, trust consented, the pinned version enforced."""
    return GrokAgentConfig(
        settings_overrides=build_stub_custom_model_settings_overrides(stub.get_api_base_url()),
        version=read_pinned_grok_version(),
        auto_allow_permissions=True,
        auto_dismiss_dialogs_at_startup=True,
        env=tuple(f"{name}={value}" for name, value in environment.items()),
    )
