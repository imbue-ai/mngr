# mngr-minds-eval

Eval harness for Minds. Given a personas file, creates one Modal workspace per
`persona x trial` from the forever-claude-template autosend branch, all in the one Minds
dashboard. Each workspace's in-sandbox `chat_watcher` (shipped on the FCT
`minds-eval-autosend` branch) then delivers that persona's `first_prompt` as the user, once
its agent finishes the opening turn.

Runs inside the minds-box container (the create API and `git_url` are container-local):

```
uv run --package mngr-minds-eval mngr-minds-eval /work/personas.json \
    --branch minds-eval-autosend -n 1
```

Personas file — a list (or `{"personas": [...]}`) of `{"id", "persona", "first_prompt"}`.
Only `first_prompt` is required; `id` is slugified for the workspace name.

`--self-check` runs the offline asserts (payload builder, persona loader, trial expansion).
