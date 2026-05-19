"""Tests for OVH provider backend registration + F1 source-position invariant."""

import inspect
import re
from pathlib import Path

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_ovh import backend as backend_module
from imbue.mngr_ovh.backend import OVH_BACKEND_NAME
from imbue.mngr_ovh.backend import OvhProvider
from imbue.mngr_ovh.backend import OvhProviderBackend
from imbue.mngr_ovh.backend import register_provider_backend
from imbue.mngr_ovh.config import OvhProviderConfig


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
    assert "--vps-datacenter" in help_text
    assert "--vps-plan" in help_text
    assert "--vps-os" in help_text


def test_backend_start_args_help() -> None:
    assert "docker run" in OvhProviderBackend.get_start_args_help()


def test_register_provider_backend_returns_tuple() -> None:
    result = register_provider_backend()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] is OvhProviderBackend
    assert result[1] is OvhProviderConfig


# -- F1 invariant -------------------------------------------------------------
#
# Constructing a full ``OvhProvider`` for a behaviour-level test is
# expensive (it inherits from ``VpsDockerProvider`` which inherits from
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
