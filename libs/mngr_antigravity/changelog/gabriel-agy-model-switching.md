The statusline now records agy's reasoning effort in `model_state.json` alongside the model. As of agy 1.1.5, effort is its own setting (`/effort`, `--effort`), and the statusline payload reports it. When it does, the effort is written as `effort` and the matching tier suffix is dropped from the model name, so `Gemini 3.8 Flash (High)` is recorded as `{"model": "Gemini 3.8 Flash", "effort": "high"}`. A model whose effort cannot be changed reports no effort and is recorded under its full name, for example `Claude Sonnet 4.6 (Thinking)` or `GPT-OSS 120B (Medium)`.

Sending a slash command such as `/model` to an antigravity agent now returns as soon as agy has handled it. agy never goes busy for a slash command, so the send used to wait out the full 15-second relaxed confirmation window every time. Such sends now also count as confirmed once agy's statusline rewrites `model_state.json`.

The README's `model` example now names a current model (`Gemini 3.8 Flash (Medium)`).
