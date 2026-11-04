# Config and launch fixtures

Files the pinned grok 1.0.62 binary consumed or wrote, used by `grok_config_test.py` and `resources/grok_launch_test.py`.
Every file was captured against a local stub model server; none comes from a real account.

| File | What it shows |
|---|---|
| `config_full_isolation.toml` | A per-agent `config.toml` with codebase upload and workspace teleport off, `permission_mode = "ask"`, a custom stub model, the Claude isolation marker, every `[compat.claude]` and `[compat.cursor]` cell off and `[compat.codex] sessions = false`. `grok inspect --json` reported exactly two config warnings for it, both `unknown-field` (`claude_compat` and `privacy`, keys grok itself writes). The `[ui.status_line]` command path is replaced with `/fixture/statusline_silent.sh`. |
| `hooks_mngr.json` | The hooks file that ran `./grok_hook.py` beside it directly (not through `sh`) for all 15 events, in 77 of 77 hook runs. |
| `config_after_consent_accept.toml` | The `config.toml` grok rewrote after a consent notice was accepted: the `[consent.answers.<id>]` table with `version`, `account` and `acked`, plus the keys grok adds on that rewrite. The account is the stub's placeholder login. |
| `session_buckets.json` | The 15 session bucket directories grok created, one per work dir: plain, dotted, space, NFC and NFD unicode, symlink and `/tmp` alias (stored as the canonical path), two punctuation sets, the 255/256-byte boundary and three long-form names. |
| `role_prompt_with_frontmatter.md` | A role file starting with `---` frontmatter and holding double quotes, `$` forms, backticks and consecutive blank lines. Passed as `"--rules=$(cat role_prompt_with_frontmatter.md)"`, it reached grok verbatim as one argument, without its trailing newline. |

Normalisation in `session_buckets.json`: the scratch directory prefix of each captured path is replaced with a placeholder of the same length and the same number of `/`, built from unreserved characters (`/private/tmp/fixture-00/fixture-000…`), and the same substitution is applied to the encoded prefix of each observed bucket.
Percent-encoding maps each unreserved character to itself, so every observed name stays exact and every encoded length is unchanged, which keeps the 255/256-byte boundary cases on the right side.
Long-form names hash the original path, so those three cases record only `is_long_form: true`.
