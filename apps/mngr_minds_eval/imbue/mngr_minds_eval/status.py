"""Read eval status straight from S3 -- no running box, no live sandboxes needed."""

from __future__ import annotations

from imbue.mngr_minds_eval import s3_store


def list_batches() -> None:
    env = s3_store.load_aws_env()
    client = s3_store.make_client(env)
    batches = s3_store.list_batches(client, env["MINDS_EVAL_BUCKET"])
    if not batches:
        print("no eval batches in s3://{}".format(env["MINDS_EVAL_BUCKET"]))
        return
    print("{:<28} {:<18} {:>6}".format("EVAL", "CREATED", "CASES"))
    for batch in batches:
        config = s3_store.get_json(client, env["MINDS_EVAL_BUCKET"], "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME))
        name, stamp = s3_store.split_batch(batch)
        cases = len(config.get("cases", [])) if config else 0
        print("{:<28} {:<18} {:>6}".format(name[:28], stamp, cases or "?"))
    print("\ninspect a batch:  minds-evals inspect <EVAL>_<CREATED>")


def inspect(batch: str) -> None:
    env = s3_store.load_aws_env()
    client = s3_store.make_client(env)
    bucket = env["MINDS_EVAL_BUCKET"]
    config = s3_store.get_json(client, bucket, "{}/{}".format(batch, s3_store.BATCH_CONFIG_NAME))
    if config is None:
        print("no such batch: {} (try: minds-evals list-batches)".format(batch))
        return

    eval_name = config.get("eval_name", "")
    num_turns = config.get("num_turns", "?")
    print("batch {}   turns: {}   compute: {}".format(batch, num_turns, config.get("compute", "?")))
    print("{:<26} {:<12} {:>10}  {}".format("CASE", "STATE", "TURNS", "TRANSCRIPT"))

    finished = 0
    for case in config.get("cases", []):
        case_id = case["id"]
        prefix = s3_store.case_prefix(batch, eval_name, case_id)
        state = s3_store.get_json(client, bucket, "{}/{}".format(prefix, s3_store.STATE_NAME))
        if state is None:
            print("{:<26} {:<12} {:>10}  {}".format(case_id[:26], "missing", "-", "-"))
            continue
        test_state = state.get("test_state", "?")
        finished += test_state == "finished"
        turns = "{}/{}".format(state.get("waits_done", "?"), state.get("num_turns", num_turns))
        has_transcript = _exists(client, bucket, "{}/{}".format(prefix, s3_store.TRANSCRIPT_KEY))
        print("{:<26} {:<12} {:>10}  {}".format(case_id[:26], test_state, turns, "yes" if has_transcript else "-"))

    total = len(config.get("cases", []))
    print("\n{}/{} finished".format(finished, total))


def _exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False
