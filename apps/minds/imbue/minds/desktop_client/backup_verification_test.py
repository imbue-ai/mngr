"""Unit tests for the backup-service check classification rules."""

import base64

import pytest

from imbue.minds.desktop_client.backup_env_store import env_content_sha256
from imbue.minds.desktop_client.backup_verification import BackupServiceCheckState
from imbue.minds.desktop_client.backup_verification import BackupServiceProblem
from imbue.minds.desktop_client.backup_verification import MINIMUM_BACKUP_SERVICE_TAG
from imbue.minds.desktop_client.backup_verification import MINIMUM_BACKUP_TAG_ENV_VAR
from imbue.minds.desktop_client.backup_verification import classify_check_payload
from imbue.minds.desktop_client.backup_verification import minimum_backup_tag

_CANONICAL_ENV = "RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n"


def _env_payload(content: str) -> dict[str, object]:
    return {
        "present": True,
        "sha256": env_content_sha256(content),
        "content_b64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }


def _healthy_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "target_tag": "minds-v1.2.3",
        "code_state": "matches",
        "code_detail": "",
        "installed_version": "minds-v1.2.3",
        "service_state": "running",
        "service_detail": "host-backup RUNNING",
        "env": _env_payload(_CANONICAL_ENV),
    }


def test_all_green_classifies_ok() -> None:
    check, env_to_adopt = classify_check_payload(_healthy_payload(), canonical_env=_CANONICAL_ENV)
    assert check.state == BackupServiceCheckState.OK
    assert check.problems == ()
    assert check.installed_version == "minds-v1.2.3"
    assert check.minimum_version == "minds-v1.2.3"
    assert env_to_adopt is None


def test_outdated_code_is_flagged() -> None:
    payload = _healthy_payload()
    payload["code_state"] = "outdated"
    check, _ = classify_check_payload(payload, canonical_env=_CANONICAL_ENV)
    assert check.state == BackupServiceCheckState.PROBLEMS
    assert BackupServiceProblem.CODE_OUTDATED in check.problems


def test_newer_code_is_not_flagged() -> None:
    payload = _healthy_payload()
    payload["code_state"] = "newer"
    check, _ = classify_check_payload(payload, canonical_env=_CANONICAL_ENV)
    assert check.state == BackupServiceCheckState.OK


def test_unverifiable_code_is_flagged_with_detail() -> None:
    payload = _healthy_payload()
    payload["code_state"] = "unverifiable"
    payload["code_detail"] = "git fetch official --tags failed: no network"
    check, _ = classify_check_payload(payload, canonical_env=_CANONICAL_ENV)
    assert BackupServiceProblem.UNVERIFIABLE in check.problems
    assert "no network" in check.detail


def test_service_not_running_is_flagged() -> None:
    payload = _healthy_payload()
    payload["service_state"] = "not_running"
    payload["service_detail"] = "host-backup STOPPED"
    check, _ = classify_check_payload(payload, canonical_env=_CANONICAL_ENV)
    assert BackupServiceProblem.SERVICE_NOT_RUNNING in check.problems
    assert "STOPPED" in check.detail


def test_missing_workspace_env_is_flagged_when_canonical_exists() -> None:
    payload = _healthy_payload()
    payload["env"] = {"present": False}
    check, _ = classify_check_payload(payload, canonical_env=_CANONICAL_ENV)
    assert BackupServiceProblem.ENV_MISSING in check.problems


def test_drifted_workspace_env_is_flagged_when_canonical_exists() -> None:
    payload = _healthy_payload()
    payload["env"] = _env_payload("RESTIC_REPOSITORY=s3:other\nRESTIC_PASSWORD=x\n")
    check, _ = classify_check_payload(payload, canonical_env=_CANONICAL_ENV)
    assert BackupServiceProblem.ENV_MISMATCH in check.problems


def test_no_canonical_env_and_no_workspace_env_is_not_configured() -> None:
    payload = _healthy_payload()
    payload["env"] = {"present": False}
    check, env_to_adopt = classify_check_payload(payload, canonical_env=None)
    assert BackupServiceProblem.NOT_CONFIGURED in check.problems
    assert env_to_adopt is None


def test_complete_external_env_is_adopted_instead_of_not_configured() -> None:
    external_env = "RESTIC_REPOSITORY=s3:external\nRESTIC_PASSWORD=secret\n"
    payload = _healthy_payload()
    payload["env"] = _env_payload(external_env)
    check, env_to_adopt = classify_check_payload(payload, canonical_env=None)
    assert BackupServiceProblem.NOT_CONFIGURED not in check.problems
    assert env_to_adopt == external_env


def test_incomplete_external_env_is_not_adopted() -> None:
    payload = _healthy_payload()
    # An env without RESTIC_PASSWORD is incomplete and must not be adopted.
    payload["env"] = _env_payload("RESTIC_REPOSITORY=s3:external\n")
    check, env_to_adopt = classify_check_payload(payload, canonical_env=None)
    assert BackupServiceProblem.NOT_CONFIGURED in check.problems
    assert env_to_adopt is None


def test_multiple_problems_accumulate() -> None:
    payload = _healthy_payload()
    payload["code_state"] = "outdated"
    payload["service_state"] = "not_running"
    payload["env"] = {"present": False}
    check, _ = classify_check_payload(payload, canonical_env=_CANONICAL_ENV)
    assert set(check.problems) == {
        BackupServiceProblem.CODE_OUTDATED,
        BackupServiceProblem.SERVICE_NOT_RUNNING,
        BackupServiceProblem.ENV_MISSING,
    }


def test_minimum_backup_tag_defaults_to_the_fixed_constant() -> None:
    assert minimum_backup_tag() == MINIMUM_BACKUP_SERVICE_TAG
    assert MINIMUM_BACKUP_SERVICE_TAG.startswith("minds-v")


def test_minimum_backup_tag_honors_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MINIMUM_BACKUP_TAG_ENV_VAR, "minds-v9.9.9")
    assert minimum_backup_tag() == "minds-v9.9.9"
