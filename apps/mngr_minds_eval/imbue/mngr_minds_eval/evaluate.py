"""Evaluate a finished eval batch: pull each case's transcript from S3, score it, write results back.

Add a new evaluation by writing a function that takes a `_Case` and returns a `{key: value}` dict,
then appending it to `EVALUATIONS` (and its keys to `RESULT_KEYS`). `evaluate_single_case` runs them
all and merges the dicts into `case_eval_results.json`; the batch aggregate averages every numeric
key across cases into `batch_eval_results.json`.

Host-native and S3-only (like inspect): no box, no Modal. The LLM-graded evals call the Anthropic
API, so ANTHROPIC_API_KEY must be set.
"""

from __future__ import annotations

import concurrent.futures
import json
from dataclasses import dataclass

from imbue.mngr_minds_eval import anthropic_call
from imbue.mngr_minds_eval import s3_store
from imbue.mngr_minds_eval import status

CASE_RESULTS_NAME = "case_eval_results.json"
BATCH_RESULTS_NAME = "batch_eval_results.json"


@dataclass(frozen=True)
class _Case:
    """The parsed, user-facing conversation of one case."""

    agent_turns: list[str]  # non-empty assistant messages, in order (the agent's real turns)
    conversation: str  # rendered user + agent turns, for the LLM judge


def _parse_transcript(jsonl_text: str) -> _Case:
    """full_transcript.jsonl -> the user-facing conversation.

    One JSON event per line. `user_message` carries `content`, `assistant_message` carries `text`;
    empty-text assistant events are internal (tool/thinking) placeholders, not real turns.
    """
    agent_turns: list[str] = []
    rendered: list[str] = []
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant_message":
            text = (event.get("text") or "").strip()
            if text:
                agent_turns.append(text)
                rendered.append("AGENT: {}".format(text))
        elif event.get("type") == "user_message":
            content = (event.get("content") or "").strip()
            if content:
                rendered.append("USER: {}".format(content))
    return _Case(agent_turns=agent_turns, conversation="\n\n".join(rendered))


# --- evaluations: each takes a _Case, returns {key: value}. Add one by appending to EVALUATIONS. ---


def _eval_avg_word_count(case: _Case) -> dict:
    """Average words per agent turn -- the raw verbosity signal behind conciseness_score."""
    counts = [len(turn.split()) for turn in case.agent_turns]
    return {"avg_word_count": round(sum(counts) / len(counts), 1) if counts else 0.0}


_JUDGE_PROMPT = """You are grading how an AI agent talks to a non-technical client it is building software for. Below is their conversation (USER is the client, AGENT is the agent being graded).

Answer these three questions, each on a 1-10 scale:

1. conciseness_score: How concise is the AGENT? 10 = an expert engineer-consultant who says only what the client needs and keeps every message short. 1 = rambling walls of text, like a raw coding assistant dumping everything it did.

2. nontechnical_language_score: How non-technical is the AGENT's language to the client? 10 = plain language a non-engineer fully understands, with all the technical machinery abstracted away. 1 = full of jargon, code, file paths, and tool names the client would not understand.

3. proactive_score: How self-directed is the AGENT? 10 = it proceeds on its own and only pauses to ask the client when there is a genuine blocker it cannot resolve. 1 = it stops to ask about trivial things it should have just decided.

Reply with ONLY a JSON object, no other text:
{{"conciseness_score": <int 1-10>, "nontechnical_language_score": <int 1-10>, "proactive_score": <int 1-10>}}

Transcript:
{conversation}
"""

_LLM_KEYS = ("conciseness_score", "nontechnical_language_score", "proactive_score")


def _eval_llm_scores(case: _Case) -> dict:
    """Ask Claude the three rubric questions; return the three 1-10 scores."""
    scores = _extract_json(anthropic_call.ask(_JUDGE_PROMPT.format(conversation=case.conversation)))
    return {key: scores.get(key) for key in _LLM_KEYS}


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of the reply (tolerates ```json fences and surrounding prose)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or start > end:
        raise ValueError("no JSON object in model reply: {!r}".format(text[:200]))
    return json.loads(text[start : end + 1])


EVALUATIONS = (_eval_avg_word_count, _eval_llm_scores)
RESULT_KEYS = ("avg_word_count", *_LLM_KEYS)


def evaluate_single_case(client, bucket: str, case_prefix_value: str) -> dict:
    """Pull the transcript, run every evaluation, write case_eval_results.json, return the merged dict."""
    body = (
        client.get_object(Bucket=bucket, Key="{}/{}".format(case_prefix_value, s3_store.TRANSCRIPT_KEY))["Body"]
        .read()
        .decode()
    )
    case = _parse_transcript(body)
    results: dict = {}
    for evaluation in EVALUATIONS:
        results.update(evaluation(case))
    s3_store.put_json(client, bucket, "{}/{}".format(case_prefix_value, CASE_RESULTS_NAME), results)
    return results


def _is_number(value) -> bool:
    # bool is a subclass of int -- exclude it so a stray true/false score isn't averaged as 1/0.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _aggregate(per_case: dict[str, dict]) -> dict:
    """Average each numeric result key across cases (skips keys a case failed to produce)."""
    aggregate: dict = {}
    for key in RESULT_KEYS:
        values = [c[key] for c in per_case.values() if _is_number(c.get(key))]
        aggregate[key] = round(sum(values) / len(values), 2) if values else None
    return aggregate


def evaluate_batch(batch: str) -> None:
    """Score every FINISHED case in parallel, aggregate, and write results back. Cases that aren't
    finished yet (or that error) are shown as N/A rows and left out of the aggregate -- so a batch
    with a straggler can still be evaluated for the rest."""
    env = s3_store.load_aws_env()
    client = s3_store.make_client(env)
    bucket = env["MINDS_EVAL_BUCKET"]

    config, rows = status.case_report(client, bucket, batch)
    if config is None:
        raise SystemExit("no such batch: {} (try: minds-evals list-batches)".format(batch))

    finished = [r for r in rows if (r["state"] or {}).get("test_state") == "finished"]
    timed_out = [r["id"] for r in rows if (r["state"] or {}).get("test_state") == "timed_out"]
    still_running = [r["id"] for r in rows if (r["state"] or {}).get("test_state") not in ("finished", "timed_out")]
    if not finished:
        raise SystemExit(
            "no finished cases to evaluate in {} ({} still running, {} timed out)".format(
                batch, len(still_running), len(timed_out)
            )
        )

    # Each successful case overwrites its own case_eval_results.json below, and the batch aggregate is
    # rewritten at the end -- so a re-run recomputes cleanly WITHOUT a pre-delete that would destroy
    # prior good scores if this run then fails (e.g. an expired ANTHROPIC_API_KEY or a network blip).
    print(">> evaluating {}/{} finished case(s) in {} ...".format(len(finished), len(rows), batch), flush=True)
    per_case: dict[str, dict] = {}
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(finished))) as pool:
        futures = {pool.submit(evaluate_single_case, client, bucket, r["prefix"]): r["id"] for r in finished}
        for future in concurrent.futures.as_completed(futures):
            case_id = futures[future]
            try:
                per_case[case_id] = future.result()
            except Exception as exc:  # one bad case shouldn't lose the rest
                errors[case_id] = str(exc)

    if not per_case:
        raise SystemExit("all finished cases failed to evaluate: {}".format(errors))

    batch_results = _aggregate(per_case)
    s3_store.put_json(client, bucket, "{}/{}".format(batch, BATCH_RESULTS_NAME), batch_results)
    # A row per case in config order; None -> N/A (case not finished yet, or its eval errored).
    display_rows = [(row["id"], per_case.get(row["id"])) for row in rows]
    _print_table(display_rows, batch_results)
    notes = []
    if still_running:
        notes.append("not finished: {}".format(", ".join(still_running)))
    if timed_out:
        notes.append("timed out: {}".format(", ".join(timed_out)))
    notes += ["eval error for {}: {}".format(case_id, message) for case_id, message in errors.items()]
    if notes:
        print("  N/A -- " + "; ".join(notes), flush=True)


def _cell(value) -> str:
    if value is None:
        return "-"
    return "{:.1f}".format(value) if isinstance(value, float) else str(value)


def _print_table(display_rows: list[tuple[str, dict | None]], batch_results: dict) -> None:
    """Rows = cases (a results dict, or None -> N/A), columns = the result keys, plus a BATCH AVG row."""
    name_w = max([len("CASE"), len("BATCH AVG")] + [len(cid) for cid, _ in display_rows])
    widths = {key: max(len(key), 5) for key in RESULT_KEYS}

    def _row(label: str, results: dict | None) -> str:
        cells = "".join(
            "  {:>{w}}".format("N/A" if results is None else _cell(results.get(k)), w=widths[k]) for k in RESULT_KEYS
        )
        return "{:<{w}}{}".format(label, cells, w=name_w)

    header = "{:<{w}}".format("CASE", w=name_w) + "".join("  {:>{w}}".format(k, w=widths[k]) for k in RESULT_KEYS)
    print("\n" + header)
    print("-" * len(header))
    for case_id, results in display_rows:
        print(_row(case_id, results))
    print("-" * len(header))
    print(_row("BATCH AVG", batch_results))
