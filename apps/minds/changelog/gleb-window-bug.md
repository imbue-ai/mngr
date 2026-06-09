The startup loading window no longer flashes at the default centered
position before jumping to its saved location. Saved bounds from the
previous session are now applied to the initial window before its
loading screen renders, so the loading view appears in place and no
visible jump occurs when content loads.

Window state is now persisted in most-recently-focused order, so for
multi-window users the loading screen opens at the bounds of the last
window they interacted with (rather than the oldest still-open one).
