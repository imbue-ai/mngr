import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import ContentBlock
from .models import MessageRole
from .models import ParsedMessage
from .models import SessionMetadata
from .models import TextBlock
from .models import ToolResultBlock
from .models import ToolUseBlock


class TranscriptParser:
    """Parses Claude Code JSONL transcripts into structured messages.

    Tool results from user messages are merged into the preceding assistant
    message that contains the matching tool_use, so they display together.
    """

    def __init__(self, transcript_path: Path):
        self.transcript_path = transcript_path
        self._messages: list[ParsedMessage] = []
        self._metadata: SessionMetadata | None = None
        self._last_position = 0
        self._tool_use_names: dict[str, str] = {}  # tool_use_id -> tool_name
        # Track pending tool_use IDs that need results
        self._pending_tool_uses: dict[str, int] = {}  # tool_use_id -> message index

        # Initial parse
        self._parse_file()

    def _parse_file(self) -> list[ParsedMessage]:
        """Parse file from last position, return new messages."""
        new_messages: list[ParsedMessage] = []
        # Track tool results to merge after all messages are parsed
        pending_results: list[ToolResultBlock] = []

        with open(self.transcript_path, "r") as f:
            f.seek(self._last_position)

            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                    result, tool_results = self._parse_line(data, len(self._messages) + len(new_messages))
                    if result:
                        new_messages.append(result)
                    if tool_results:
                        pending_results.extend(tool_results)
                except json.JSONDecodeError:
                    # Incomplete line, likely still being written
                    # Don't update position so we retry this line
                    break
                except Exception as e:
                    # Log but continue - don't break on malformed data
                    print(f"Warning: Failed to parse line: {e}")

            self._last_position = f.tell()

        # Add new messages first
        self._messages.extend(new_messages)

        # Now merge pending tool results into their corresponding messages
        for tool_result in pending_results:
            self._merge_tool_result(tool_result)

        return new_messages

    def _parse_line(
        self, data: dict[str, Any], current_index: int
    ) -> tuple[ParsedMessage | None, list[ToolResultBlock]]:
        """Parse a single JSONL line into a ParsedMessage and any tool results to merge."""
        line_type = data.get("type")

        # System init message
        if line_type == "system" and data.get("subtype") == "init":
            self._metadata = SessionMetadata(
                session_id=data.get("session_id", ""),
                model=data.get("model", "unknown"),
                tools=data.get("tools", []),
            )
            return None, []

        # Assistant message
        if line_type == "assistant":
            message_data = data.get("message", {})
            content_blocks = self._parse_content_blocks(message_data.get("content", []))

            # Track tool use IDs for later matching with results
            for block in content_blocks:
                if isinstance(block, ToolUseBlock):
                    self._tool_use_names[block.id] = block.name
                    self._pending_tool_uses[block.id] = current_index

            return ParsedMessage(
                id=message_data.get("id", str(uuid4())),
                role=MessageRole.ASSISTANT,
                content=content_blocks,
            ), []

        # User message (text or tool results)
        if line_type == "user":
            message_data = data.get("message", {})
            content = message_data.get("content")

            if isinstance(content, str):
                # Plain text user message
                return ParsedMessage(
                    id=str(uuid4()),
                    role=MessageRole.USER,
                    content=[TextBlock(text=content)],
                ), []
            elif isinstance(content, list):
                # Check if this is purely tool results (should be merged) or has user text
                content_blocks = self._parse_content_blocks(content)

                # Separate tool results from other content
                tool_results = [b for b in content_blocks if isinstance(b, ToolResultBlock)]
                other_content: list[ContentBlock] = [b for b in content_blocks if not isinstance(b, ToolResultBlock)]

                # Only create a user message if there's non-tool-result content
                if other_content:
                    return ParsedMessage(
                        id=str(uuid4()),
                        role=MessageRole.USER,
                        content=other_content,
                    ), tool_results

                # Pure tool results - return them for merging, no user message needed
                return None, tool_results

        # Result message - session complete
        if line_type == "result":
            # Could emit metadata but for now we skip
            return None, []

        return None, []

    def _merge_tool_result(self, tool_result: ToolResultBlock) -> None:
        """Merge a tool result into the assistant message that has the matching tool_use."""
        tool_use_id = tool_result.tool_use_id

        if tool_use_id in self._pending_tool_uses:
            msg_index = self._pending_tool_uses[tool_use_id]
            if msg_index < len(self._messages):
                # Append tool result to the assistant message's content
                self._messages[msg_index].content.append(tool_result)
            # Remove from pending
            del self._pending_tool_uses[tool_use_id]

    def _parse_content_blocks(self, blocks: list[dict[str, Any]]) -> list[ContentBlock]:
        """Parse raw content blocks into typed blocks."""
        parsed: list[ContentBlock] = []

        for block in blocks:
            block_type = block.get("type")

            if block_type == "text":
                text = block.get("text", "")
                if text:  # Skip empty text blocks
                    parsed.append(TextBlock(text=text))

            elif block_type == "tool_use":
                parsed.append(
                    ToolUseBlock(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input", {}),
                    )
                )

            elif block_type == "tool_result":
                content = block.get("content", "")
                # Normalize content to string for display
                if isinstance(content, list):
                    # Extract text from content items
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif isinstance(item, str):
                            text_parts.append(item)
                    content = "\n".join(text_parts)
                elif not isinstance(content, str):
                    content = str(content)

                parsed.append(
                    ToolResultBlock(
                        tool_use_id=block.get("tool_use_id", ""),
                        content=content,
                        is_error=block.get("is_error", False),
                    )
                )

        return parsed

    def get_messages(self) -> list[ParsedMessage]:
        """Get all parsed messages."""
        return self._messages.copy()

    def get_metadata(self) -> SessionMetadata | None:
        """Get session metadata."""
        return self._metadata

    def get_tool_name(self, tool_use_id: str) -> str:
        """Get tool name for a tool_use_id."""
        return self._tool_use_names.get(tool_use_id, "Tool")

    def parse_updates(self) -> list[ParsedMessage]:
        """Parse new content since last read."""
        return self._parse_file()
