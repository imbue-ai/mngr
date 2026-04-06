import uvicorn

from imbue.claude_web_chat.config import load_config
from imbue.claude_web_chat.server import create_application


def main() -> None:
    """Run the claude-web-chat server."""
    config = load_config()
    application = create_application(config)
    uvicorn.run(application, host=config.claude_web_chat_host, port=config.claude_web_chat_port)


if __name__ == "__main__":
    main()
