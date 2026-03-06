class SlackExporterError(Exception):
    """Base exception for all slack_exporter errors."""

    ...


class LatchkeyInvocationError(SlackExporterError, RuntimeError):
    """Raised when a latchkey subprocess call fails."""

    def __init__(self, command: str, return_code: int, stderr: str) -> None:
        self.command = command
        self.return_code = return_code
        self.stderr = stderr
        super().__init__(f"latchkey command failed (exit {return_code}): {command}\n{stderr}")


class SlackApiError(SlackExporterError, RuntimeError):
    """Raised when the Slack API returns an error response."""

    def __init__(self, method: str, error: str) -> None:
        self.method = method
        self.error = error
        super().__init__(f"Slack API error in {method}: {error}")


class ChannelNotFoundError(SlackExporterError, KeyError):
    """Raised when a channel name cannot be resolved to a channel ID."""

    def __init__(self, channel_name: str) -> None:
        self.channel_name = channel_name
        super().__init__(f"Channel not found: {channel_name}")
