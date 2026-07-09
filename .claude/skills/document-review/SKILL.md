---
name: document-review
version: 1.2.0
description: >
  Pull a paper, grant, or data-access application from the review coordinator,
  read the linked source documents (PubMed/MEDLINE/PMC/NIH RePORTER/bioRxiv),
  run YOUR local versioned prompt, and submit the structured JSON review back.
---

# Document Review Skill

You are a reviewer worker for a central coordination server. **The prompt lives
here, in `./prompts` (one file per version).** The client hashes the active
prompt and sends that hash to the server when leasing work. The server tracks
replication per **(item, prompt)** — so you can run the whole corpus under
several prompts and complete each one independently.

The **server chooses which item you get** — you never pass an ID. `GET /next`
leases one for your active prompt; `POST /reviews` receives the result.

## When to use
When the user asks to "run the next document", "review the next item", "process
the review queue", or "score papers/grants from the review server".

## Setup (once)
1. `pip install -r requirements.txt` (installs `requests`, `pyyaml`, `anthropic`).
2. Copy `skill_config.example.yaml` to `skill_config.yaml`; set `server_url` to
   your coordinator's **IP:port**, a stable `worker_id`, and `prompt_version`
   (the file stem in `prompts/`).
3. Export `ANTHROPIC_API_KEY` (used by `run` / `auto`).
4. Recommended for polite/faster NCBI access: set `NCBI_API_KEY` and `NCBI_EMAIL`.

## Primary workflow: one-shot
```
python client.py run
```
No arguments. In a single call this:
1. leases one item from `server_url` `/next` for the active prompt (its hash is
   sent automatically — the server picks the ID),
2. downloads every linked document's text from the public sources,
3. runs `prompt.text` over the concatenated document context via the Anthropic
   API and parses the JSON,
4. `POST`s the review back to `/reviews` with the prompt version+hash and worker
   metadata (host, run id, skill version, Python version, timings), and
5. prints a one-object status report: `item_id`, `assignment_id`, `parsed_ok`,
   `http_status`, `server_status`, `warnings`.

If there's nothing to do it prints `status: "no_work"` with `prompt_complete`
(true → this prompt is finished; false → items are leased to others, retry
shortly). Submitting is idempotent per assignment, so a retry after a network
blip won't double-count.

**Report the printed status object to the user.** To drain the queue, run it
again until it reports `prompt_complete: true`, or use batch mode below.

## Batch mode
```
python client.py auto --max-items N
```
Same fetch → API → submit loop as `run`, repeated up to N times (or until the
prompt is complete). Good for unattended draining; for a scheduled/cron job you
can call this directly without the skill layer.

## Manual mode (Claude-in-the-loop, no API key)
If you'd rather the reviewing model be *this* session rather than a separate API
call, split it:
1. `python client.py fetch` — prints the work packet
   (`{assignment_id, item_id, prompt{version,hash,text}, documents[], context, ...}`).
2. Apply `packet.prompt.text` to `packet.context` yourself and write the JSON to
   `result.json`, basing every field strictly on the provided documents.
3. `python client.py submit --assignment-id "<id>" --item-id "<id>" --result result.json`

## Running multiple prompts
Drop a new file in `prompts/` (e.g. `triage_v2.txt`), set
`prompt_version: triage_v2`, and run the loop again. The server treats it as
separate work and drives it to completion independently; each prompt reports its
own `prompt_complete`.

## Notes
- `run`/`auto` make a **separate** Anthropic API call per item (fresh context,
  its own token cost). Invoking `/document-review` inside a Claude session means
  that session triggers the script, which in turn calls the API — if you don't
  want the nested call, use manual mode instead.
- The slash command follows the skill `name` (`/document-review`). To type
  `/run-document` literally, rename this skill's folder and `name:` to
  `run-document`.
- Always submit against the `assignment_id` you were given — the server keys
  replication accounting off the lease's prompt hash (not any client value), so
  reviews can't be mis-attributed.
- Watch `server_prompt_echo.version_conflict`: if true, the same version *label*
  has been seen under a different hash — usually a prompt file edited without
  bumping its version name.
- Documents may return an `error` (e.g. not in the PMC open-access subset, or a
  bioRxiv full-text miss). Review from whatever text resolved and lower
  `confidence` if the material is thin.
- Supported document sources: `pubmed`, `medline`, `pmc`, `nih_reporter`,
  `biorxiv`, `medrxiv`, and `text` (inline).
