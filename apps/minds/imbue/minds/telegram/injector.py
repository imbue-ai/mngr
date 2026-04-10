"""Inject Telegram bot credentials into a running mngr agent."""

import shlex
from typing import Final

from loguru import logger
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.errors import MngrCommandError
from imbue.mngr.primitives import AgentId

_SECRETS_FILE: Final[str] = "runtime/secrets"


def inject_telegram_bot_token(
    agent_id: AgentId,
    bot_token: SecretStr,
) -> None:
    """Inject a Telegram bot token into an agent's runtime/secrets file.

    Uses ``mngr exec`` to write the token into the agent's secrets file,
    which the agent's bootstrap service manager will pick up.

    Raises MngrCommandError if the mngr exec command fails.
    """
    safe_token = shlex.quote(bot_token.get_secret_value())
    with log_span("Injecting Telegram bot token into agent {}", agent_id):
        cg = ConcurrencyGroup(name="mngr-exec-telegram-token")
        with cg:
            command = [
                MNGR_BINARY,
                "exec",
                str(agent_id),
                f"mkdir -p runtime && printf 'export TELEGRAM_BOT_TOKEN=%s\\n' {safe_token} >> {_SECRETS_FILE}",
            ]
            result = cg.run_process_to_completion(
                command=command,
                is_checked_after=False,
            )

        if result.returncode != 0:
            error_detail = result.stderr.strip() if result.stderr.strip() else result.stdout.strip()
            raise MngrCommandError(f"Failed to inject Telegram bot token into agent {agent_id}: {error_detail}")

    logger.debug("Injected Telegram bot token into agent {}", agent_id)
