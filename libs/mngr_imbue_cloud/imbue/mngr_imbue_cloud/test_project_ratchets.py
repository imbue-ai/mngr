"""Project-specific guardrails for the imbue_cloud plugin's wire-parsing discipline.

The connector client parses responses produced by servers that deploy
independently of this (shipped) code, so every parse must go through the
tolerant ``validate_wire`` / ``parse_wire_entries`` entrypoints in
``wire.py`` -- they are typed to accept only WireModel subclasses and add the
drift observability and list semantics the forward-compatibility contract
requires. See ``wire.py`` and the connector's ``wire_compat_test.py``.
"""

import re
from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent


def test_connector_client_never_calls_model_validate_directly() -> None:
    """connector/client.py must parse response bodies via validate_wire, never Model.model_validate.

    ``validate_wire`` is typed to accept only WireModel subclasses, so routing
    every parse through it is what guarantees no strict (extra="forbid") model
    can ever validate a connector response body again.
    """
    client_source = (_PACKAGE_DIR / "connector" / "client.py").read_text()
    direct_calls = re.findall(r"\.model_validate\(", client_source)
    assert len(direct_calls) == 0, (
        "connector/client.py calls model_validate directly; parse connector responses through "
        "validate_wire / parse_wire_entries (wire.py) instead, so only WireModel subclasses can "
        "ever validate wire bodies."
    )
