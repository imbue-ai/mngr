"""Offline unit checks for the pure CLI helpers (no box, no S3, no Modal)."""

import json
import tempfile
from pathlib import Path

from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import workspace
from imbue.mngr_minds_eval.launch import load_cases

_ENV = {"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK", "AWS_DEFAULT_REGION": "us-east-1",
        "MINDS_EVAL_BUCKET": "b"}


def test_s3_prefixes() -> None:
    assert s3_store.batch_prefix("web1", "20260713-101500") == "web1_20260713-101500"
    assert s3_store.split_batch("web1_20260713-101500") == ("web1", "20260713-101500")
    assert s3_store.case_prefix("web1_S", "web1", "todo") == "web1_S/web1_todo"
    assert s3_store.restic_repo_url(_ENV, "web1_S/web1_todo") == \
        "s3:s3.us-east-1.amazonaws.com/b/web1_S/web1_todo/restic"


def test_launch_case_payload_is_modal_apikey_configure_later() -> None:
    # The payload launch builds per case (via workspace.build_payload).
    payload = workspace.build_payload(
        fct_link="/work/clones/todo", fct_branch="", name="EVAL-web1-CASE-todo", compute="modal",
        ai_provider="api_key", anthropic_key="sk-ant", backup_provider="configure_later",
    )
    assert payload["launch_mode"] == "MODAL"
    assert payload["ai_provider"] == "API_KEY"
    assert payload["backup_provider"] == "CONFIGURE_LATER"
    assert "backup_api_key_env" not in payload  # we don't send a restic password; worker owns it
    assert payload["branch"] == "" and payload["git_url"] == "/work/clones/todo"


def test_load_cases_ok_and_rejects_empty_prompt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.json"
        path.write_text(json.dumps([{"id": "a", "persona": "p", "first_prompt": "go"}]))
        assert load_cases(path) == [{"id": "a", "persona": "p", "first_prompt": "go"}]
        path.write_text(json.dumps([{"id": "a", "first_prompt": " "}]))
        try:
            load_cases(path)
            raise AssertionError("expected ValueError on empty first_prompt")
        except ValueError:
            pass
