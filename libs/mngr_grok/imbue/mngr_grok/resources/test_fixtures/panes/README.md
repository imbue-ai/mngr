# Grok Build pane captures

Plain `tmux capture-pane -p` text of grok's terminal UI, produced by grok **1.0.62** (`@xai-official/grok`) in a private tmux server.
`grok_screen_test.py` reads every capture through `manifest.toml`, which records what each predicate in `grok_screen.py` must return for it.

## How the captures were made

- Pane size: 170x48 by default; names starting `w100`, `g100` or `p100` are 100x30, names starting `w80` are 80x24.
- Model and login: a local stub model named `Stub` behind an API key, except for the `gates_` captures, which use an auth-provider command against a local stub of xAI's endpoints with the model `grok-4.6`.
- Launch: `-s <session id>` by default; `--resume <session id>` for `*_i_resume_*` and `sessions_q5_quit_resume_*`; a bare `grok` (no session flag) for `g170_welcome_*`, `gates_*`, `trust_*` and `claudeiso_*`.

## Normalisation

- The capturing scratch directory is replaced by `/tmp/scratch`; shell command lines that held it are re-wrapped at the pane width, with blank rows keeping the row count.
- Clock times in the transcript read `0:00 PM`.
- The header path (`/p/t/c/-/9/s/p/i/work/w170`) is grok's own abbreviation of the work directory and is kept.

## What each capture shows

Width-prefixed captures (`w170_`, `w100_`, `w80_`) come from one scripted session per width:

| Suffix | Screen |
|---|---|
| `k_launch_01` | the shell line running the launcher, before grok draws |
| `k_launch_03` (170, 100) | `-s` launch with the `Starting session…` spinner row |
| `w80_k_launch_03` | the one-frame welcome screen (`Logged in with API key`) of a `-s` launch |
| `k_launch_04` (170), `k_launch_05` (80) | first ready frame of the `-s` launch |
| `a_fresh_t05` | fresh session 5 s after launch, before any prompt |
| `b_fresh_typed`, `d_idle_typed` | text typed into an idle composer (`Enter:send`) |
| `c_idle_after_turn` | idle after one turn |
| `d_idle_typed_ctrlc_01`, `d_idle_after_ctrlc_t3` | Ctrl+C cleared the typed text |
| `e_busy_empty` | a turn running, empty composer (`Ctrl+c:cancel`) |
| `f_busy_typed` | a turn running, text in the composer (`Enter:queue`); hints are cut from the right at 100 and 80 columns |
| `g_busy_queued1`, `g_busy_queued2` | one and two queued follow-ups (`#N` rows, `Enter:send now`) |
| `g_busy_queued1_typed` | one queued follow-up and a second message typed in the composer |
| `g_after_queue_drained` | idle after both queued follow-ups ran |
| `h_ctrlq_01`, `h_ctrlq_02` | one Ctrl+Q (`Ctrl+q:press again to quit`), then the next frame, after the confirmation lapsed |
| `h_after_quit_shell` | the shell after grok quit |
| `i_resume_03` | `--resume` launch showing `Loading session <id>...` |
| `i_resume_04`, `i_resume_t10` | the resumed transcript, ready |

Other captures from the input check (170x48 unless named otherwise):

| Capture | Screen |
|---|---|
| `g170_welcome_t05` | bare launch: the welcome screen, last line `Logged in with API key` |
| `g170_welcome_typed`, `g170_welcome_after_turn` | text typed on the welcome screen; idle after that turn |
| `g100_notrust_launch_03` | the one-frame welcome screen of a `-s` launch at 100 columns |
| `g170_trust_dialog_t05`, `g100_trust_dialog_t05` | the folder-trust dialog |
| `g170_trust_after_yes_01`, `g170_trust_after_yes_t4` | `Starting session…` right after answering `y`; ready 4 s later |
| `int_5a_busy_typed_before_ctrlc`, `int_5a_after_ctrlc_01`, `int_5a_turn_finished` | Ctrl+C with text in the composer during a turn: cleared (`Input cleared` toast), turn still running, then finished |
| `int_5b_gap0.3_after_01`, `int_5b_gap0.3_after_02` | a second Ctrl+C 0.3 s after a cancelling one: `press again to quit`, then the next frame, after the confirmation lapsed |
| `int_5b_tool_busy` | a foreground tool running (`Ctrl+b:send to bg`) |
| `int_5c_idle_ctrlc_01`, `int_5c_idle_ctrlc_02` | Ctrl+C on an idle composer, then the next frame, after the confirmation lapsed |
| `int_5c_after_fast_double_press` | the shell after two quick Ctrl+C presses quit grok |
| `int_5d_busy_queued`, `int_5d_after_ctrlc_01`, `int_5d_settled` | Ctrl+C with a follow-up queued: the queued message starts at once, its toast stays without the row |
| `p170_paste_six_line_over_1024_composer`, `p100_…` | a 6-line bracketed paste as the chip `[Pasted: 6 lines]` under a preview box |
| `p170_paste_four_line_paste_composer` | a 4-line paste as `[Pasted: 4 lines]` |
| `p170_paste_three_line_paste_composer`, `p170_paste_one_line_1500_composer` | a 3-line paste and a 1507-character line, shown inline |
| `p170_keys_five_line_under_1024_composer` | five lines sent with `send-keys -l`, shown inline on five composer rows |
| `p170_busy_paste_six_chip`, `p100_…` | the 6-line chip in the composer of a busy session with two follow-ups queued |
| `p170_queued_long_single`, `p100_…` | a long single-line follow-up queued, its row cut with `…` |
| `p170_queued_multi` | a three-line follow-up queued as `MULTIQ first line (+2 lines)` |
| `p170_queued_paste_six`, `p100_…` | the 6-line paste queued as its first line plus `(+5 lines)` |
| `p170_queue_drained` | idle after the queue ran |
| `perm_card`, `perm_card_after_typing` | a permission card in place of the composer (`1/4:select`); the same card after text was typed at it |
| `perm_after_escape` | the permission card with the scrollback focused (`Tab/Space:permission`) |
| `perm_after_ctrlc` | after Ctrl+C on the card: scrollback focused, the composer showing the placeholder `Build anything` |
| `perm2_sb_after_paste`, `perm2_sb_after_enter` | a paste landing in the composer from scrollback focus; idle after it ran |
| `focus_after_escape`, `focus_after_typing` | Esc on an idle composer (no focus change); text typed after it |
| `term2_notmux_plain_idle` | grok started without `TMUX` in its environment (`Ctrl+.:shortcuts`) |

Captures from the other checks:

| Capture | Screen |
|---|---|
| `gates_consent_notice_default_title_and_label` | the terms notice (`Got it  a`, `Quit  q`) |
| `gates_consent_notice_after_typed_text_and_enter` | the terms notice unchanged after typed text and Enter |
| `gates_consent_accepted_welcome_empty_composer` | the fresh session after accepting; last line the composer border |
| `gates_login_screen_after_paywall_l_logout` | the login screen (`Login with Grok  l`) with the placeholder composer |
| `gates_paywall_free_tier_default_cta`, `gates_paywall_free_tier_custom_label` | the subscription paywall (`Tier: Free  [Refresh]`, `Logout  ctrl+l`) |
| `gates_paywall_lifted_after_server_upgrade` | the fresh session after the server lifted the paywall |
| `gates_recipe_b_welcome_fresh_session_ready` | a fresh session logged in through the auth provider: no footer, last line the composer border `╰…─ grok-4.6 ─╯` |
| `gates_recipe_b_welcome_text_typed`, `gates_recipe_b_idle_after_first_turn` | text typed there; idle after that turn |
| `trust_plain_none_startup` | the folder-trust dialog for an unseeded repository |
| `sessions_q4_plain_ready`, `sessions_q4_long_unicode_ready` | `-s` sessions ready before the first prompt, one in a work directory with a long Unicode name |
| `sessions_q5_quit_resume_before_prompt` | a never-prompted session resumed after a quit |
| `sessions_q8_N1_typed_new` | `/new` typed, with the slash-command list above the composer |
| `claudeiso_pane_permission_dialog_run_terminal_command` | a permission card for a terminal command |

Captures from the dropped-cancel check (200x50, the release tests' pane size), each after one Ctrl+C before the
first reply of a turn, which grok drops and whose prompt it puts back in the composer:

| Capture | Screen |
|---|---|
| `drop_restored_one_line`, `drop_restored_five_lines` | idle, the restored one-line and five-line prompt in the composer (`Enter:send`) |
| `drop_restored_pasted_six_lines` | idle, a restored 6-line pasted prompt as the chip `[Pasted: 6 lines]` under its preview box |
| `drop_after_clearing_ctrlc` | 1 s after a second Ctrl+C cleared the restored chip (`Input cleared` toast): idle and empty |

`messages/` holds the exact text of the long, multi-line and pasted messages whose queued rows the `p*` captures show.
