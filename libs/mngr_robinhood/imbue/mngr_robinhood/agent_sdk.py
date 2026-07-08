"""mngr-backed re-implementation of the Claude Agent SDK Python surface.

Importing from here is meant to be a drop-in replacement for ``claude_agent_sdk``: callers
import ``query`` / ``ClaudeAgentOptions`` / ``ClaudeSDKClient`` (and the session functions)
from ``imbue.mngr_robinhood.agent_sdk`` instead of from ``claude_agent_sdk``.

The behavioral entry points (``query``, ``ClaudeSDKClient``, and the session functions) are
re-implemented on top of mngr: each session is a ``robinhood-``-prefixed mngr claude agent,
driven through the in-process mngr API and read back from its native transcript. Every
*type* (options, messages, content blocks, session info, permission/hook types) is re-exported
verbatim from ``claude_agent_sdk`` so that ``isinstance`` checks and field shapes are identical
across the two implementations.

This module lives at the package root (rather than inside the ``imbue`` namespace tree) only
in the import sense: the real import path is ``imbue.mngr_robinhood.agent_sdk``.
"""

# --- Options (re-exported verbatim; reused as-is by the mngr-backed driver) -----------------
from claude_agent_sdk import AgentDefinition as AgentDefinition

# --- Message + content-block types (re-exported so isinstance is identical) -----------------
from claude_agent_sdk import AssistantMessage as AssistantMessage

# --- Error types (re-exported) --------------------------------------------------------------
from claude_agent_sdk import CLIConnectionError as CLIConnectionError
from claude_agent_sdk import CLINotFoundError as CLINotFoundError

# --- Permission + hook types (re-exported; wiring is implemented incrementally) -------------
from claude_agent_sdk import CanUseTool as CanUseTool
from claude_agent_sdk import ClaudeAgentOptions as ClaudeAgentOptions
from claude_agent_sdk import ClaudeSDKError as ClaudeSDKError
from claude_agent_sdk import ContentBlock as ContentBlock
from claude_agent_sdk import HookContext as HookContext
from claude_agent_sdk import HookInput as HookInput
from claude_agent_sdk import HookJSONOutput as HookJSONOutput
from claude_agent_sdk import HookMatcher as HookMatcher
from claude_agent_sdk import Message as Message
from claude_agent_sdk import PermissionMode as PermissionMode
from claude_agent_sdk import PermissionResult as PermissionResult
from claude_agent_sdk import PermissionResultAllow as PermissionResultAllow
from claude_agent_sdk import PermissionResultDeny as PermissionResultDeny
from claude_agent_sdk import ProcessError as ProcessError
from claude_agent_sdk import ResultMessage as ResultMessage

# --- Session types (re-exported) ------------------------------------------------------------
from claude_agent_sdk import SDKSessionInfo as SDKSessionInfo
from claude_agent_sdk import SessionMessage as SessionMessage
from claude_agent_sdk import SettingSource as SettingSource
from claude_agent_sdk import StreamEvent as StreamEvent
from claude_agent_sdk import SystemMessage as SystemMessage
from claude_agent_sdk import TextBlock as TextBlock
from claude_agent_sdk import ThinkingBlock as ThinkingBlock
from claude_agent_sdk import ToolPermissionContext as ToolPermissionContext
from claude_agent_sdk import ToolResultBlock as ToolResultBlock
from claude_agent_sdk import ToolUseBlock as ToolUseBlock
from claude_agent_sdk import UserMessage as UserMessage

# --- Behavioral entry points (mngr-backed; the reason this module exists) -------------------
from imbue.mngr_robinhood._agent_sdk.client import ClaudeSDKClient as ClaudeSDKClient
from imbue.mngr_robinhood._agent_sdk.client import query as query
from imbue.mngr_robinhood._agent_sdk.sessions import get_session_info as get_session_info
from imbue.mngr_robinhood._agent_sdk.sessions import get_session_messages as get_session_messages
from imbue.mngr_robinhood._agent_sdk.sessions import list_sessions as list_sessions
from imbue.mngr_robinhood._agent_sdk.sessions import rename_session as rename_session
from imbue.mngr_robinhood._agent_sdk.sessions import tag_session as tag_session
