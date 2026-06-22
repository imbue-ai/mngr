"""Tests for OVH provider backend registration + F1 source-position invariant."""

import inspect
import os
import re
from pathlib import Path

import ovh.config
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_ovh import backend as backend_module
from imbue.mngr_ovh.backend import OVH_BACKEND_NAME
from imbue.mngr_ovh.backend import OvhProvider
from imbue.mngr_ovh.backend import OvhProviderBackend
from imbue.mngr_ovh.backend import register_provider_backend
from imbue.mngr_ovh.config import OvhProviderConfig

# python-ovh resolves credentials from these fixed files (no env override exists for
# the path). If any are present on the host, an "unconfigured" config could still pick
# up real credentials, so the unconfigured-provider test below skips in that case.
_OVH_CONFIG_FILE_PATHS: tuple[str, ...] = tuple(ovh.config.CONFIG_PATH)

_OVH_CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "OVH_ENDPOINT",
    "OVH_APPLICATION_KEY",
    "OVH_APPLICATION_SECRET",
    "OVH_APP_KEY",
    "OVH_APP_SECRET",
    "OVH_CONSUMER_KEY",
    "OVH_CLIENT_ID",
    "OVH_CLIENT_SECRET",
)


def test_backend_name() -> None:
    assert OvhProviderBackend.get_name() == ProviderBackendName("ovh")


def test_backend_name_constant() -> None:
    assert OVH_BACKEND_NAME == ProviderBackendName("ovh")


def test_backend_description() -> None:
    desc = OvhProviderBackend.get_description()
    assert "OVH" in desc
    assert "Docker" in desc


def test_backend_config_class() -> None:
    assert OvhProviderBackend.get_config_class() is OvhProviderConfig


def test_backend_build_args_help() -> None:
    help_text = OvhProviderBackend.get_build_args_help()
    assert "--ovh-datacenter" in help_text
    assert "--ovh-plan" in help_text


def test_backend_start_args_help() -> None:
    assert "docker run" in OvhProviderBackend.get_start_args_help()


def test_register_provider_backend_returns_tuple() -> None:
    result = register_provider_backend()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] is OvhProviderBackend
    assert result[1] is OvhProviderConfig


def test_build_provider_instance_raises_not_authorized_when_unconfigured(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled-but-unconfigured OVH provider surfaces as ProviderNotAuthorizedError, not a silent empty listing."""
    # Skip if the host has a real OVH config file: python-ovh would then resolve real
    # credentials and the provider would (correctly) construct rather than raise.
    if any(os.path.exists(path) for path in _OVH_CONFIG_FILE_PATHS):
        pytest.skip("an OVH config file exists on this host, so credentials are resolvable")
    for env_var in _OVH_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    name = ProviderInstanceName("ovh-no-creds")
    config = OvhProviderConfig(backend=OVH_BACKEND_NAME)

    with pytest.raises(ProviderNotAuthorizedError) as exc_info:
        OvhProviderBackend.build_provider_instance(name, config, temp_mngr_ctx)

    # A ProviderUnavailableError subclass so read paths keep the provider visible
    # (reported as unavailable) rather than dropping it from the listing.
    assert isinstance(exc_info.value, ProviderUnavailableError)
    assert exc_info.value.provider_name == name


# -- F1 invariant -------------------------------------------------------------
#
# Constructing a full ``OvhProvider`` for a behaviour-level test is
# expensive (it inherits from ``VpsProvider`` which inherits from
# ``BaseProviderInstance`` and pulls in ``MngrContext`` etc.). The
# parsing-failure path itself is already exhaustively covered by
# ``iam_tags_test.py::test_parse_extra_tags_env_*``. What F1 added is a
# strict source-position contract: the parse MUST happen BEFORE the
# recycle attempt and BEFORE ``order_and_wait_for_vps`` in
# ``_provision_vps`` so a malformed ``MNGR_VPS_EXTRA_TAGS`` cannot leak
# a freshly-ordered month of OVH billing. Pin that contract here via a
# source-text analysis so a future refactor that moves the parse back
# down silently breaks this test.


def test_f1_extra_tags_parsed_before_recycle_or_order() -> None:
    """F1: ``parse_extra_tags_env`` runs BEFORE ``_maybe_claim_recycled_vps`` and ``order_and_wait_for_vps``.

    Before the F1 fix the parse ran AFTER ``order_and_wait_for_vps``, so a
    typo in ``MNGR_VPS_EXTRA_TAGS`` (uppercase key, reserved key, missing
    ``=``) raised only after we'd already ordered + paid for a VPS. The
    spec explicitly required pre-order parsing. This test pins the
    source-line invariant so a future refactor can't reintroduce the
    bug silently.
    """
    source = Path(inspect.getsourcefile(OvhProvider) or "").read_text()
    provision_match = re.search(r"def _provision_vps\(.*?(?=\n    def |\n\nclass |\Z)", source, re.DOTALL)
    assert provision_match is not None, "could not locate _provision_vps in backend.py source"
    body = provision_match.group(0)

    parse_pos = body.find("parse_extra_tags_env(")
    recycle_pos = body.find("_maybe_claim_recycled_vps(")
    order_pos = body.find("order_and_wait_for_vps(")

    assert parse_pos != -1, "OvhProvider._provision_vps must call parse_extra_tags_env(...) -- F1 invariant"
    assert recycle_pos != -1, "OvhProvider._provision_vps must call _maybe_claim_recycled_vps(...)"
    assert order_pos != -1, "OvhProvider._provision_vps must call order_and_wait_for_vps(...)"

    assert parse_pos < recycle_pos, (
        f"F1 violation: parse_extra_tags_env (pos {parse_pos}) must appear before "
        f"_maybe_claim_recycled_vps (pos {recycle_pos}) in _provision_vps -- "
        "see F1 in OVH_AUDIT.md. A malformed MNGR_VPS_EXTRA_TAGS otherwise "
        "raises after we've already done IAM mutations on a candidate VPS."
    )
    assert parse_pos < order_pos, (
        f"F1 violation: parse_extra_tags_env (pos {parse_pos}) must appear before "
        f"order_and_wait_for_vps (pos {order_pos}) in _provision_vps -- "
        "see F1 in OVH_AUDIT.md. A malformed MNGR_VPS_EXTRA_TAGS otherwise "
        "raises after we've already ordered (and paid for) a fresh OVH VPS."
    )


def test_f1_provision_vps_imports_parse_extra_tags_env() -> None:
    """Sanity: the backend module imports ``parse_extra_tags_env`` so the F1 fix works at all."""
    assert hasattr(backend_module, "parse_extra_tags_env")


# -- Pending-order reconciliation invariants ----------------------------------
#
# ``_reconcile_pending_orders`` MUST run at the top of ``_provision_vps``,
# specifically BEFORE ``_maybe_claim_recycled_vps``, so any orphan that
# was delivered between bakes is tagged + cancelled in time for the
# recycle path to claim it as a candidate for the CURRENT bake. If
# reconcile ran AFTER recycle, an orphan adopted this bake would only
# be eligible on the NEXT bake -- the immediate recovery the design
# promises wouldn't happen. Same source-text pinning style as F1.


def test_reconcile_pending_orders_runs_before_recycle_check() -> None:
    """Source-position invariant: ``_reconcile_pending_orders`` precedes ``_maybe_claim_recycled_vps``.

    Catches a future refactor that accidentally moves the reconcile
    sweep after the recycle check (which would defer orphan recovery
    by one full bake cycle).
    """
    source = Path(inspect.getsourcefile(OvhProvider) or "").read_text()
    provision_match = re.search(r"def _provision_vps\(.*?(?=\n    def |\n\nclass |\Z)", source, re.DOTALL)
    assert provision_match is not None, "could not locate _provision_vps in backend.py source"
    body = provision_match.group(0)

    reconcile_pos = body.find("_reconcile_pending_orders(")
    recycle_pos = body.find("_maybe_claim_recycled_vps(")
    order_pos = body.find("order_and_wait_for_vps(")

    assert reconcile_pos != -1, "OvhProvider._provision_vps must call _reconcile_pending_orders() at startup"
    assert recycle_pos != -1, "OvhProvider._provision_vps must call _maybe_claim_recycled_vps(...)"
    assert order_pos != -1, "OvhProvider._provision_vps must call order_and_wait_for_vps(...)"

    assert reconcile_pos < recycle_pos, (
        f"Invariant violation: _reconcile_pending_orders (pos {reconcile_pos}) must run BEFORE "
        f"_maybe_claim_recycled_vps (pos {recycle_pos}) in _provision_vps. If reconcile runs "
        "after recycle, any orphan adopted this bake won't be eligible until the NEXT bake -- "
        "the immediate same-bake recovery the design promises wouldn't happen."
    )
    assert reconcile_pos < order_pos, (
        f"Invariant violation: _reconcile_pending_orders (pos {reconcile_pos}) must run BEFORE "
        f"order_and_wait_for_vps (pos {order_pos}) in _provision_vps. Otherwise the bake places "
        "a fresh order even when an adoptable orphan is available."
    )


def test_provision_vps_writes_marker_on_delivery_timeout() -> None:
    """Source-position invariant: the ``OvhOrderDeliveryTimeoutError`` except block calls ``write_pending_order_marker``.

    The reconcile sweep is useless if the failure path doesn't deposit
    a marker for it to find. Pin that call here so a refactor can't
    silently drop the marker write.
    """
    source = Path(inspect.getsourcefile(OvhProvider) or "").read_text()
    provision_match = re.search(r"def _provision_vps\(.*?(?=\n    def |\n\nclass |\Z)", source, re.DOTALL)
    assert provision_match is not None
    body = provision_match.group(0)
    timeout_except_pos = body.find("except OvhOrderDeliveryTimeoutError")
    marker_write_pos = body.find("write_pending_order_marker(")
    assert timeout_except_pos != -1, "_provision_vps must catch OvhOrderDeliveryTimeoutError"
    assert marker_write_pos != -1, "_provision_vps must write a pending-order marker on timeout"
    assert timeout_except_pos < marker_write_pos < len(body), (
        "write_pending_order_marker must appear inside the OvhOrderDeliveryTimeoutError except block"
    )
