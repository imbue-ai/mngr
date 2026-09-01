The pre-ATIF transitional state is gone from the common transcript.

The retired `user_message` / `assistant_message` / `tool_result` record types have been removed from the canonical schema: the common transcript is now exactly the ATIF-shaped stream (`header` / `step` / `observation`).

`mngr transcript` renders only ATIF-shaped records. A stream written by an agent provisioned before the ATIF cutover is detected and reported with a clear error ("its stream uses the retired pre-ATIF format") for every output format, instead of being partially rendered. The check runs before role filtering, so it fires whatever `--role` was passed.

`--role` help and examples drop the "legacy streams use assistant" hedge, and the tutorial's "view only the agent's own messages" block is back to `mngr transcript my-task --role agent`.

The old-format error now names the recourse: the raw native transcript is still there under `logs/<agent_type>_transcript/`, and `mngr event <agent>` still shows the raw records. The reader and the ATIF doc-builder share one explanation of why the stream cannot be read.

`mngr transcript` warns when a `--role` filter matches nothing on a stream that does have records, naming the requested roles and the valid vocabulary (`user`, `agent`, `system`, `tool`), instead of printing an empty transcript. Its help text describes the stream in ATIF terms (user turns, agent turns, and tool results).
