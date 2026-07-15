"""Offline unit checks for the pure CLI helpers (no box, no S3, no Modal)."""

import json
import tempfile
from pathlib import Path

from imbue.mngr_minds_eval import box
from imbue.mngr_minds_eval import main
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import workspace
from imbue.mngr_minds_eval.launch import load_config
from imbue.mngr_minds_eval.launch import normalize_cases
from imbue.mngr_minds_eval.launch import validate_name

_ENV = {
    "AWS_ACCESS_KEY_ID": "AK",
    "AWS_SECRET_ACCESS_KEY": "SK",
    "AWS_DEFAULT_REGION": "us-east-1",
    "MINDS_EVAL_BUCKET": "b",
}


def test_s3_prefixes() -> None:
    assert s3_store.case_prefix("web1", "web1", "todo") == "web1/web1_todo"
    assert s3_store.restic_repo_url(_ENV, "web1/web1_todo") == "s3:s3.us-east-1.amazonaws.com/b/web1/web1_todo/restic"


def test_launch_case_payload_is_modal_apikey_configure_later() -> None:
    # The payload launch builds per case (via workspace.build_payload).
    payload = workspace.build_payload(
        fct_link="/work/clones/todo",
        fct_branch="",
        name="EVAL-web1-CASE-todo",
        ai_provider="api_key",
        anthropic_key="sk-ant",
        backup_provider="configure_later",
    )
    assert payload["launch_mode"] == "MODAL"
    assert payload["ai_provider"] == "API_KEY"
    assert payload["backup_provider"] == "CONFIGURE_LATER"
    assert "backup_api_key_env" not in payload  # we don't send a restic password; worker owns it
    assert payload["branch"] == "" and payload["git_url"] == "/work/clones/todo"


def test_normalize_cases_ok_and_validates_prompts() -> None:
    assert normalize_cases(
        [{"id": "a", "persona": "p", "prompts": ["go", "Sounds good.", "DECIDE_FROM_PERSONA"]}]
    ) == [{"id": "a", "persona": "p", "prompts": ["go", "Sounds good.", "DECIDE_FROM_PERSONA"]}]
    for bad in (
        [{"id": "a", "prompts": []}],  # empty prompts
        [{"id": "a", "prompts": ["go", " "]}],  # empty prompt element
        [{"id": "a", "prompts": ["DECIDE_FROM_PERSONA", "go"]}],
    ):  # decide can't be first
        try:
            normalize_cases(bad)
            raise AssertionError("expected ValueError for {!r}".format(bad))
        except ValueError:
            pass


def test_load_config_validates_required_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "eval.json"
        path.write_text(
            json.dumps({"name": "web1", "mngr_branch": "minds-eval", "personas": [{"id": "a", "prompts": ["go"]}]})
        )
        config = load_config(path)
        assert config["name"] == "web1"
        path.write_text(json.dumps({"name": "web1", "personas": []}))  # missing mngr_branch
        try:
            load_config(path)
            raise AssertionError("expected SystemExit on missing mngr_branch")
        except SystemExit:
            pass


def test_box_naming_and_env_derivation() -> None:
    # The eval NAME is the batch identity: env and container names key on it directly.
    name = "trio"
    assert box.sanitize_user_id(name) == name
    assert box.modal_env_name(name) == "minds-staging-trio"
    ref = "38f9311059b9deadbeef0000"
    assert box.container_name(name, ref, desktop=False) == "minds-box-trio-38f9311059b9"
    assert box.container_name(name, ref, desktop=True) == "minds-box-trio-38f9311059b9-desktop"


def test_sanitize_user_id_edge_cases() -> None:
    # Weird characters collapse to single dashes; overlong input is capped; garbage-only input raises.
    assert box.sanitize_user_id("My Batch!!__(v2)") == "my-batch-v2"
    assert len(box.sanitize_user_id("x" * 100)) <= 40
    try:
        box.sanitize_user_id("___")
        raise AssertionError("expected BoxError for a garbage-only id")
    except box.BoxError:
        pass


def test_point_arg_to_box_rewrites_all_forms() -> None:
    local = Path("eval-config.json")
    dest = "/work/eval-config.json"
    argv = ["launch", "--config", "./eval-config.json", "--config=eval-config.json", "--other", "x"]
    out = main._point_arg_to_box(argv, local, dest)
    assert out == ["launch", "--config", dest, "--config={}".format(dest), "--other", "x"]


def test_validate_name_rejects_invalid_names() -> None:
    # The name IS the batch id (S3 prefix + Modal env): lowercase alnum + dashes, at most 40 chars.
    assert validate_name("trio") == "trio"
    for bad in ("My Eval", "x" * 41, "UPPER", "under_score"):
        try:
            validate_name(bad)
            raise AssertionError("expected SystemExit for name {!r}".format(bad))
        except SystemExit as exc:
            assert "lowercase" in str(exc)


def test_load_config_requires_template_keys() -> None:
    # The config is a reusable template: no 'name' (given at launch); branch + personas required.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"personas": [{"id": "a", "prompts": ["hi"]}]}, f)
        path = Path(f.name)
    try:
        load_config(path)
        raise AssertionError("expected SystemExit for a config without mngr_branch")
    except SystemExit as exc:
        assert "mngr_branch" in str(exc)
