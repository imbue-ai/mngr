"""Project-specific ratchets for the minds app.

Lives outside ``test_ratchets.py`` because that file must define the same
test set across every project (enforced by ``test_meta_ratchets.py``).
"""

from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet

_DIR = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

_RAW_POST_MESSAGE_RULE = RatchetRuleInfo(
    rule_name="raw postMessage / message-listener usages outside the embed contract",
    rule_description=(
        "All chrome<->workspace (and chrome<->modal-iframe) messaging must flow through the embed "
        "contract module (desktop_client/static/embed_contract.js) or the shell bridge it backs, so "
        "the whole message surface stays in one auditable place with the contract's source checks "
        "and payload validation applied (see docs/embed-contract.md). Do not call postMessage or "
        "register 'message' listeners directly -- extend the contract instead."
    ),
)

# Allowlist-by-file: any NEW file that touches the raw primitives fails
# immediately (the snapshot is pinned at 0 with these exclusions).
_ALLOWED_POST_MESSAGE_FILES = (
    # The contract itself: the one sanctioned home for the primitives.
    "embed_contract.js",
    # Vendored third-party bundle (Sentry's browser SDK).
    "sentry.browser.min.js",
    # Tests stand in windows/listeners to exercise the boundary itself.
    "test/unit/*",
    "test/e2e/*",
)

_POST_MESSAGE_PATTERN = RegexPattern(r"""postMessage\(|addEventListener\(\s*["']message["']""", multiline=False)


def test_prevent_raw_post_message_outside_embed_contract() -> None:
    chunks = []
    for extension in (".js", ".html", ".jinja"):
        chunks.extend(
            check_regex_ratchet(_DIR, FileExtension(extension), _POST_MESSAGE_PATTERN, _ALLOWED_POST_MESSAGE_FILES)
        )
    assert len(chunks) <= snapshot(0), _RAW_POST_MESSAGE_RULE.format_failure(tuple(chunks))
