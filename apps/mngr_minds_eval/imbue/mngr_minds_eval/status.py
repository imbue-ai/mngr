"""Read eval status straight from S3 -- no running box, no live sandboxes needed."""

from __future__ import annotations

from imbue.mngr_minds_eval import launch as launch_mod
from imbue.mngr_minds_eval import s3_store


def list_batches() -> None:
    env = s3_store.load_aws_env()
    client = s3_store.make_client(env)
    batches = s3_store.list_batches(client, env["MINDS_EVAL_BUCKET"])
    if not batches:
        print("no eval batches in s3://{}".format(env["MINDS_EVAL_BUCKET"]))
        return
    print("{:<40} {:>6}  {}".format("BATCH (pass this to inspect)", "CASES", "CREATED"))
    for batch in batches:
        config = s3_store.get_json(client, env["MINDS_EVAL_BUCKET"], "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME))
        _, stamp = s3_store.split_batch(batch)
        cases = len(config.get("personas", [])) if config else 0
        print("{:<40} {:>6}  {}".format(batch, cases or "?", stamp))
    print("\ninspect a batch:  minds-evals inspect <BATCH>")


def case_report(client, bucket: str, batch: str) -> tuple[dict | None, list[dict]]:
    """(config, rows) for a batch. Each row is {id, prefix, state} where state is the parsed
    state.json or None. (None, []) when the batch does not exist. Shared by inspect and evaluate."""
    config = s3_store.get_json(client, bucket, "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME))
    if config is None:
        return None, []
    eval_name = config.get("name") or s3_store.split_batch(batch)[0]
    rows = []
    for index, case in enumerate(config.get("personas", [])):
        # Same id derivation launch used when writing, so id-less personas still resolve to their prefix.
        case_id = launch_mod.derive_case_id(case, index)
        prefix = s3_store.case_prefix(batch, eval_name, case_id)
        state = s3_store.get_json(client, bucket, "{}/{}".format(prefix, s3_store.STATE_NAME))
        rows.append({"id": case_id, "prefix": prefix, "state": state})
    return config, rows


def inspect(batch: str) -> None:
    env = s3_store.load_aws_env()
    client = s3_store.make_client(env)
    bucket = env["MINDS_EVAL_BUCKET"]
    config, rows = case_report(client, bucket, batch)
    if config is None:
        print("no such batch: {} (try: minds-evals list-batches)".format(batch))
        return

    print("batch {}   mngr: {}@{}".format(batch, config.get("mngr_branch", "?"), (config.get("mngr_sha") or "")[:12]))
    print("{:<26} {:<12} {:>10}  {}".format("CASE", "STATE", "TURNS", "TRANSCRIPT"))

    finished = 0
    for row in rows:
        case_id, prefix, state = row["id"], row["prefix"], row["state"]
        if state is None:
            print("{:<26} {:<12} {:>10}  {}".format(case_id[:26], "missing", "-", "-"))
            continue
        test_state = state.get("test_state", "?")
        finished += test_state == "finished"
        turns = "{}/{}".format(state.get("waits_done", "?"), state.get("num_turns", "?"))
        has_transcript = _exists(client, bucket, "{}/{}".format(prefix, s3_store.TRANSCRIPT_KEY))
        print("{:<26} {:<12} {:>10}  {}".format(case_id[:26], test_state, turns, "yes" if has_transcript else "-"))

    print("\n{}/{} finished".format(finished, len(rows)))


def _exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False
