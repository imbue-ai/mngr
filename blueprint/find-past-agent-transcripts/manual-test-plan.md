# Manual test plan: find past agents' transcripts

How to manually verify the feature end-to-end, as a real user/agent would. The
automated tests cover the pure logic and the API handlers (via the Flask test
client against real on-disk preserved files); this plan covers the parts that
need a running stack: a real destroyed agent, the latchkey gateway, and the FCT
skill.

## A. mngr layer (local, no minds app needed)

Goal: confirm a destroyed agent's transcript lands in the preserved store and the
new helpers read it.

1. Create a local agent and give it a transcript:
   - `uv run mngr create --type claude --name manual-transcript-test ...` (use your usual local provider).
   - Send it a message or two so it produces a common transcript
     (`uv run mngr message manual-transcript-test "hello"`), then confirm a live
     transcript renders: `uv run mngr transcript manual-transcript-test`.
2. Destroy it: `uv run mngr destroy manual-transcript-test` (keep the default
   `preserve_sessions_on_destroy`).
3. Confirm the durable copy exists on disk:
   - `ls ~/.mngr/preserved/manual-transcript-test--*/events/*/common_transcript/events.jsonl`
4. Confirm the new helpers see it (the path `mngr transcript` itself does NOT
   read, which is the whole reason for these helpers):
   ```bash
   uv run python -c "
   from pathlib import Path
   from imbue.mngr.api.preservation import list_preserved_agents
   from imbue.mngr.api.transcript import render_preserved_agent_transcript
   from imbue.mngr.primitives import OutputFormat
   host = Path('~/.mngr').expanduser()
   agents = list_preserved_agents(host)
   print('preserved:', [(str(a.agent_name), str(a.agent_id), a.preserved_at.isoformat()) for a in agents])
   target = next(a for a in agents if str(a.agent_name) == 'manual-transcript-test')
   print(render_preserved_agent_transcript(host, target.agent_id, (), None, None, OutputFormat.HUMAN))
   "
   ```
   Expected: the agent appears in the preserved list (newest-first), and the
   transcript text matches what `mngr transcript` showed before destroy.
5. Filters: re-run with `('user',), 5, None, OutputFormat.JSONL` and confirm role
   filtering + head slicing behave like `mngr transcript --role user --head 5 --format jsonl`.

Tear-down: `rm -rf ~/.mngr/preserved/manual-transcript-test--*`.

## B. minds API (running desktop client)

Goal: confirm the two endpoints serve real data through the bare-origin API.

Pre-req: a `minds run` instance with at least one destroyed-and-preserved
workspace (destroy a workspace from the app, or reuse step A's host_dir if minds
points at it via `mngr_host_dir`).

1. Get the bearer key / use the desktop session cookie. As the desktop UI does,
   call with the session cookie; as an agent does, call via the gateway (see C).
2. List preserved agents:
   ```bash
   curl -s -H "Authorization: Bearer $MINDS_API_KEY" \
     http://127.0.0.1:<minds-port>/api/v1/workspaces/preserved | jq
   ```
   Expected: `{"agents": [{agent_id, agent_name, preserved_at}, ...]}`, newest-first.
3. Read one transcript:
   ```bash
   curl -s -H "Authorization: Bearer $MINDS_API_KEY" \
     "http://127.0.0.1:<minds-port>/api/v1/workspaces/<AGENT_ID>/transcript?format=jsonl&role=user&tail=20" | jq
   ```
   Expected: `{agent_id, format:"jsonl", is_preserved:true, content:"<jsonl>"}` with
   only user events, last 20.
4. Negative cases:
   - Unknown id (random valid AgentId, never preserved, not live) -> `404`.
   - Malformed id (`/workspaces/not-an-id/transcript`) -> `400`.
   - `?format=yaml` -> `400`.
   - `?head=1&tail=1` -> `400`.
   - No auth header / cookie -> `401`.
5. Live fallback: pick a *live* workspace id (one with no preserved copy) and fetch
   its transcript; expect `is_preserved:false` and content matching
   `mngr transcript <id>`.

## C. Agent-in-VM path (latchkey gateway + FCT skill)

Goal: confirm an agent inside a workspace can use the `find-past-transcripts`
skill (requires the FCT changes to be deployed to the workspace's template).

1. In a workspace agent, confirm the read grant or request it (per the `minds-api`
   skill): the first call returns 403 until `minds-workspaces-read` is granted.
2. List + read through the gateway (note `latchkey curl`, not plain curl):
   ```bash
   latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces/preserved | jq '.agents'
   latchkey curl "http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces/<AGENT_ID>/transcript" | jq -r '.content'
   ```
3. Trigger discovery: in a fresh workspace agent, ask "what did the agent that did
   <X> do?" and confirm the agent reaches for the `find-past-transcripts` skill
   (its description triggers on past-work phrasing), lists preserved agents,
   matches by name/time, and reads the right transcript.

Note: C depends on the forever-claude-template changes being released into the
workspace template, so it is verifiable only after the FCT branch is pushed and a
minds release picks it up.
