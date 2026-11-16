"""Helpers shared by the imbue_cloud connector unit tests."""

import base64
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone


def make_jwt_with_payload(payload: bytes) -> str:
    """A synthetic, unsigned three-part JWT carrying ``payload`` verbatim as its claims segment."""
    return f"e30.{base64.urlsafe_b64encode(payload).rstrip(b'=').decode()}.sig"


def make_jwt_expiring_in(seconds: float) -> str:
    """A synthetic, unsigned JWT whose only claim is an ``exp`` the given number of seconds from now."""
    exp = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp()
    return make_jwt_with_payload(json.dumps({"exp": exp}).encode())
