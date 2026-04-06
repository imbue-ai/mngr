"""Tests for the main entry point."""

from unittest.mock import patch

from imbue.claude_web_chat.config import Config


def test_main_starts_server() -> None:
    """main() creates an app and starts uvicorn."""
    with (
        patch("imbue.claude_web_chat.main.load_config") as mock_load_config,
        patch("imbue.claude_web_chat.main.create_application") as mock_create_app,
        patch("imbue.claude_web_chat.main.uvicorn") as mock_uvicorn,
    ):
        mock_config = Config()
        mock_load_config.return_value = mock_config
        mock_create_app.return_value = "fake_app"

        from imbue.claude_web_chat.main import main

        main()

        mock_load_config.assert_called_once()
        mock_create_app.assert_called_once_with(mock_config)
        mock_uvicorn.run.assert_called_once_with(
            "fake_app",
            host="127.0.0.1",
            port=8000,
        )
