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

# Per-secret env file inside the agent's runtime/secrets/ directory. Each
# secret writer (this token, cloudflare_tunnel.env, restic.env) owns its own
# file so they never clobber one another.
_SECRETS_FILE: Final[str] = "runtime/secrets/telegram.env"


def inject_telegram_bot_token(
    agent_id: AgentId,
    bot_token: SecretStr,
) -> None:
    """Inject a Telegram bot token into an agent's runtime/secrets/telegram.env.

    Uses ``mngr exec`` to write the token into the agent's per-secret env file.
    Overwrites any prior value in place.

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
                f"mkdir -p runtime/secrets && printf 'export TELEGRAM_BOT_TOKEN=%s\\n' {safe_token} > {_SECRETS_FILE}",
            ]
            result = cg.run_process_to_completion(
                command=command,
                is_checked_after=False,
            )

        if result.returncode != 0:
            error_detail = result.stderr.strip() if result.stderr.strip() else result.stdout.strip()
            raise MngrCommandError(f"Failed to inject Telegram bot token into agent {agent_id}: {error_detail}")

    logger.debug("Injected Telegram bot token into agent {}", agent_id)
