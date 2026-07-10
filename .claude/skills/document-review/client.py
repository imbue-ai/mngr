"""
client.py -- the worker/skill side of the review system.

The PROMPT lives here (in ./prompts, one file per version). The client computes
the prompt's SHA-256, sends it to the server as ``prompt_hash`` when leasing
work, and records it with every review. Replication is tracked per (item,
prompt_hash) on the server, so you can drive several prompts to completion.

Manual mode only (no ANTHROPIC_API_KEY / direct-API path): Claude-in-the-loop is
the reviewer, so a run only ever spends Claude subscription quota.

Subcommands:
  fetch   Lease one item *for the active prompt*, pull its document text, and
          print a work packet (JSON) with the local prompt + documents. Claude
          reads this, runs the prompt, then calls `submit`.
  submit  POST a review result for a leased assignment, with metadata (host,
          run id, skill version, timings, prompt version + hash).

Config: skill_config.yaml (see skill_config.example.yaml). Env overrides:
REVIEW_SERVER_URL, REVIEW_WORKER_ID.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
import uuid

import requests

try:
    import yaml
except ImportError:
    yaml = None

import sources
from prompts import PromptStore

SKILL_VERSION = "1.2.0"
RUN_ID = uuid.uuid4().hex
HOST = socket.gethostname()


def load_cfg(path):
    cfg = {
        "server_url": os.getenv("REVIEW_SERVER_URL", "http://api.elsichecklist.org"),
        "worker_id": os.getenv("REVIEW_WORKER_ID", f"{HOST}:{os.getpid()}"),
        "count": 1,
        "item_type": None,
        "prompts_dir": "./prompts",
        "prompt_version": "triage_v1",
    }
    if path and yaml and os.path.exists(path):
        with open(path) as fh:
            cfg.update(yaml.safe_load(fh) or {})
    return cfg


def active_prompt(cfg) -> dict:
    """{'version','hash','text'} for the prompt this worker runs."""
    return PromptStore(cfg["prompts_dir"], cfg["prompt_version"]).active()


# --------------------------------------------------------------------------- #
# Server I/O
# --------------------------------------------------------------------------- #
def get_work(cfg, prompt) -> dict:
    params = {
        "prompt_hash": prompt["hash"],
        "prompt_version": prompt["version"],
        "count": cfg["count"],
        "worker": cfg["worker_id"],
    }
    if cfg.get("item_type"):
        params["type"] = cfg["item_type"]
    r = requests.get(f"{cfg['server_url']}/next", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def submit_review(cfg, *, assignment, prompt, result, started_at, finished_at) -> dict:
    payload = {
        "assignment_id": assignment["assignment_id"],
        "item_id": assignment.get("item_id"),
        "prompt_version": prompt["version"],
        "prompt_hash": prompt["hash"],
        "result": result,
        "started_at": started_at,
        "finished_at": finished_at,
        "worker": {
            "id": cfg["worker_id"],
            "host": HOST,
            "run_id": RUN_ID,
            "skill_version": SKILL_VERSION,
            "python": platform.python_version(),
        },
    }
    r = requests.post(f"{cfg['server_url']}/reviews", json=payload, timeout=30)
    return {"http_status": r.status_code, "body": r.json()}


# --------------------------------------------------------------------------- #
# Packet assembly
# --------------------------------------------------------------------------- #
def build_packet(work, prompt) -> list[dict]:
    """Turn a /next response into self-contained packets, attaching the LOCAL
    prompt text (the server only echoes hash/version)."""
    packets = []
    for a in work["assignments"]:
        docs = sources.fetch_documents(a["documents"])
        context = "\n\n".join(
            f"===== DOCUMENT {i + 1} ({d['source']}:{d['external_id']}) =====\n"
            + (d["text"] if d["text"] else f"(fetch error: {d['error']})")
            for i, d in enumerate(docs)
        )
        packets.append({
            "assignment_id": a["assignment_id"],
            "item_id": a["item_id"],
            "item_type": a["item_type"],
            "prompt": prompt,                 # {version, hash, text} -- local source of truth
            "server_prompt_echo": work["prompt"],   # {hash, version, version_conflict, ...}
            "documents": docs,
            "context": context,
            "meta": a.get("meta", {}),
        })
    return packets


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_fetch(cfg, args):
    prompt = active_prompt(cfg)
    work = get_work(cfg, prompt)
    if not work["assignments"]:
        print(json.dumps({"assignments": [], "prompt_complete": work["prompt_complete"],
                          "prompt": work["prompt"]}))
        return
    print(json.dumps(build_packet(work, prompt), indent=2))


def cmd_submit(cfg, args):
    prompt = active_prompt(cfg)
    raw = sys.stdin.read() if args.result == "-" else open(args.result).read()
    result = json.loads(raw)
    assignment = {"assignment_id": args.assignment_id, "item_id": args.item_id}
    out = submit_review(cfg, assignment=assignment, prompt=prompt, result=result,
                        started_at=args.started_at, finished_at=time.time())
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Review skill client")
    ap.add_argument("--config", default="skill_config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fetch", help="lease work for the active prompt and print a work packet")

    ps = sub.add_parser("submit", help="submit a review result")
    ps.add_argument("--assignment-id", required=True)
    ps.add_argument("--item-id", default=None)
    ps.add_argument("--started-at", type=float, default=None)
    ps.add_argument("--result", default="-", help="path to result JSON, or - for stdin")

    args = ap.parse_args()
    cfg = load_cfg(args.config)
    {"fetch": cmd_fetch, "submit": cmd_submit}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
