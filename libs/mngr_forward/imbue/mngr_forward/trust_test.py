"""Unit tests for the CA trust-store installer (``--trust-ca``).

Only the deterministic error paths are exercised: the real macOS / Linux
install branches would mutate the developer's actual trust stores, so they are
deliberately not run here.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_forward.errors import ForwardTrustError
from imbue.mngr_forward.trust import _run_trust_command
from imbue.mngr_forward.trust import install_ca_into_trust_stores


def test_install_raises_when_ca_cert_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "ca_cert.pem"
    with ConcurrencyGroup(name=f"trust-test-{uuid4().hex}") as cg:
        with pytest.raises(ForwardTrustError, match="not found"):
            install_ca_into_trust_stores(cg, missing)


def test_install_raises_on_unsupported_platform(tmp_path: Path) -> None:
    cert = tmp_path / "ca_cert.pem"
    cert.write_bytes(b"not-really-a-cert")
    with ConcurrencyGroup(name=f"trust-test-{uuid4().hex}") as cg:
        with pytest.raises(ForwardTrustError, match="not supported on Windows"):
            install_ca_into_trust_stores(cg, cert, system="Windows")


def test_run_trust_command_wraps_command_failure(tmp_path: Path) -> None:
    """A trust command that exits non-zero surfaces as ForwardTrustError, not ProcessError."""
    with ConcurrencyGroup(name=f"trust-test-{uuid4().hex}") as cg:
        with pytest.raises(ForwardTrustError, match="Trust-store command failed"):
            _run_trust_command(cg, ["false"])


def test_run_trust_command_wraps_missing_binary(tmp_path: Path) -> None:
    """A nonexistent trust tool (OSError) surfaces as ForwardTrustError too."""
    with ConcurrencyGroup(name=f"trust-test-{uuid4().hex}") as cg:
        with pytest.raises(ForwardTrustError, match="Trust-store command failed"):
            _run_trust_command(cg, [f"/nonexistent-trust-tool-{uuid4().hex}"])
