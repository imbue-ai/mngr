`mngr upgrade` covers pi-coding agents. It rewrites the agent's lifecycle extension, `commands/mngr_pi_lifecycle.ts`; `APPEND_SYSTEM.md`, pi's settings, auth, trust and session store, and every other file stay as they are. An idle agent restarts and resumes its pi session. The upgrade is `ready` once the restarted extension writes its readiness sentinel; otherwise it fails at `ready`.

- An agent whose launch command does not load the extension, or does not resume its pi session (`resume_session = false`, or `--no-session` among its arguments), is skipped as unsupported until `mngr create NAME --reuse --update` re-creates it; for a session it does not resume, the line first names the setting or argument to change.

- pi reports no dialog and no work between turns, so an agent running a `!` command or a process it started reads as idle, and its restart ends that work.
