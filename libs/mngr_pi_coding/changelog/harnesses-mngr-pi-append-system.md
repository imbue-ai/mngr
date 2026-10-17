The pi-coding agent type now accepts `output_style` and `append_system_prompt`, matching
codex. At provision, the appended-prompt blocks and the output-style file's body (resolved
from `.agents/output-styles/`, verbatim) are written to `APPEND_SYSTEM.md` in the per-agent
pi config dir, which pi appends to its system prompt every turn -- the pi analogue of codex
writing `developer_instructions` into `config.toml`. This unblocks creating pi chat agents
(`mngr create --type pi -t chat`), whose `chat` role sets `output_style`.
