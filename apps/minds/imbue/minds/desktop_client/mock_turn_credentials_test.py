from collections.abc import Callable
from datetime import datetime
from datetime import timedelta

from pydantic import Field
from pydantic import PrivateAttr

from imbue.minds.desktop_client.api_models import IceServer
from imbue.minds.desktop_client.turn_credentials import TurnCredential
from imbue.minds.desktop_client.turn_credentials import TurnCredentialClientInterface

TEST_TURN_TTL = timedelta(hours=1)


def make_test_credential(issued_at: datetime, username: str = "u-1") -> TurnCredential:
    """A credential of the connector's shape, one hour long, telling itself apart by ``username``."""
    return TurnCredential(
        ice_servers=(
            IceServer(urls=["stun:stun.cloudflare.com:3478"]),
            IceServer(
                urls=["turn:turn.cloudflare.com:3478?transport=udp", "turns:turn.cloudflare.com:443?transport=tcp"],
                username=username,
                credential="secret-" + username,
            ),
        ),
        issued_at=issued_at,
        expires_at=issued_at + TEST_TURN_TTL,
    )


class ScriptedTurnCredentialClient(TurnCredentialClientInterface):
    """Mints through a script the test supplies (which may raise), recording every account and workspace asked for."""

    mint_fn: Callable[[str], TurnCredential] = Field(frozen=True, description="Answers (or raises for) each mint")

    _mints: list[tuple[str, str | None]] = PrivateAttr(default_factory=list)

    def mint_credential(self, user_id: str, workspace_id: str | None) -> TurnCredential:
        self._mints.append((user_id, workspace_id))
        return self.mint_fn(user_id)

    def minted_for(self) -> list[str]:
        """The account of every mint, in order."""
        return [user_id for user_id, _workspace_id in self._mints]

    def minted_for_workspaces(self) -> list[tuple[str, str | None]]:
        """Every (account, workspace host id) mint, in order."""
        return list(self._mints)
