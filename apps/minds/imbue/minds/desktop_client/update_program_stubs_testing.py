"""Stand-ins for the in-workspace tools the update program shells out to, as program text.

Test-only. Each is written to disk as an executable by ``stage_update_workspace`` in
``testing.py`` and runs as its own process, so its prints are what the update program reads.
The ``exclude`` list in ``apps/minds/pyproject.toml`` names this file to keep it out of the
shipped app.
"""

from typing import Final

# A stand-in for ``mngr`` (and ``tk``): records its argv, lists the staged agents, and names a
# created agent the way ``mngr create --format jsonl`` does.
STUB_MNGR_SOURCE: Final[str] = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_CALLS_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
command = sys.argv[1] if len(sys.argv) > 1 else ""
if command == "list":
    with open(os.environ["STUB_AGENTS_FILE"]) as handle:
        for agent in json.load(handle):
            fields = (agent["id"], agent["name"], agent["state"], agent.get("is_primary", ""), agent.get("chat_id", ""))
            print("|".join(fields))
elif command == "create":
    print(json.dumps({"event": "created", "agent_id": "agent-" + "c" * 32}))
sys.exit(int(os.environ.get("STUB_EXIT_" + command.upper(), "0")))
"""

# A stand-in for a template script: records its argv (and the text of a --message-file) and
# answers with the next scripted answer, or the last one once the script runs out.
STUB_TOOL_SOURCE: Final[str] = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
message = ""
if "--message-file" in argv:
    with open(argv[argv.index("--message-file") + 1]) as handle:
        message = handle.read()
with open(os.environ["STUB_CALLS_LOG"], "a") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0])] + argv + ([message] if message else [])) + "\n")
answers_path = sys.argv[0] + ".answers.json"
with open(answers_path) as handle:
    answers = json.load(handle)
answer = answers.pop(0) if len(answers) > 1 else answers[0]
with open(answers_path, "w") as handle:
    json.dump(answers, handle)
sys.stdout.write(answer["stdout"])
sys.stderr.write(answer["stderr"])
sys.exit(answer["exit_code"])
"""
