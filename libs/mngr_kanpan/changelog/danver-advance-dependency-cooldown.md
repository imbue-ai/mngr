Followed the annotations urwid 4.0.13 tightened, with no behaviour change to the board:

- An alarm callback's user data is now typed optional, because `set_alarm_in`'s `user_data` defaults to None. Every alarm callback accepts the optional form and checks it; every scheduling site still passes a value.

- A canvas's `coords` is now a TypedDict rather than a plain dict, and `content()` takes plain ints. The OSC 8 hyperlink canvas wrapper matches both.

- urwid_readline's keymap typing no longer needs the search-backspace suppression, which is gone.
