import argparse

import uvicorn

from imbue.claude_web_chat.config import load_config
from imbue.claude_web_chat.server import create_application


def main() -> None:
    """Run the claude-web-chat server."""
    parser = argparse.ArgumentParser(description="Claude Web Chat")
    parser.add_argument("--provider", action="append", default=[], help="Filter agents by provider name (repeatable)")
    parser.add_argument("--include", action="append", default=[], help="CEL include filter for agents (repeatable)")
    parser.add_argument("--exclude", action="append", default=[], help="CEL exclude filter for agents (repeatable)")
    args = parser.parse_args()

    config = load_config()
    application = create_application(
        config,
        provider_names=tuple(args.provider) if args.provider else None,
        include_filters=tuple(args.include),
        exclude_filters=tuple(args.exclude),
    )
    uvicorn.run(application, host=config.claude_web_chat_host, port=config.claude_web_chat_port)


if __name__ == "__main__":
    main()
