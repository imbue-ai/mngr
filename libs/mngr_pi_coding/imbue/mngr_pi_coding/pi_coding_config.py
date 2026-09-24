"""Constants and configuration paths for the Pi coding agent."""

from __future__ import annotations

from pathlib import Path
from typing import Final

# Marker file storing the ISO timestamp when the agent became idle.
IDLE_SINCE_FILENAME: Final[str] = "idle_since"

# Marker file storing the idle_since ISO timestamp for which compaction was executed.
LAST_COMPACTED_IDLE_SINCE_FILENAME: Final[str] = "last_compacted_idle_since"

# Default prompt cache TTL in minutes for Anthropic / default models.
PI_DEFAULT_CACHE_TTL_MINUTES: Final[int] = 60

# Prompt cache TTL in minutes for OpenAI models.
PI_OPENAI_CACHE_TTL_MINUTES: Final[int] = 30

# Sentinel key for compaction requests in pi_inbox.
COMPACTION_REQUEST_KEY: Final[str] = "mngr_compact"

# Output locations (under $MNGR_AGENT_STATE_DIR) for the transcript layers.
RAW_TRANSCRIPT_OUTPUT_RELATIVE: Final[Path] = Path("logs/pi-coding_transcript/events.jsonl")
COMMON_TRANSCRIPT_OUTPUT_RELATIVE: Final[Path] = Path("events/pi-coding/common_transcript/events.jsonl")
USAGE_OUTPUT_RELATIVE: Final[Path] = Path("events/pi-coding/usage/events.jsonl")

# File holding the pointer to pi's native session JSONL.
SESSION_POINTER_FILENAME: Final[str] = "pi_session_file"

# Live model state JSON file written by mngr_pi_lifecycle.ts.
MODEL_STATE_FILENAME: Final[str] = "model_state.json"

# Marker file indicating a turn is actively running.
ACTIVE_MARKER_NAME: Final[str] = "active"

# The append-only inbox file used to deliver inputs and control records.
INBOX_FILE_NAME: Final[str] = "pi_inbox"
