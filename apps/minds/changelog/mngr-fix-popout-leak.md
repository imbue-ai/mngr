Pulled-out windows no longer pile up behind the Imbue Studio window. A window you drag back also no longer pops out again on its own a few seconds later.

- A popout dropped back onto the Studio window closes as soon as that window's desktop has the window back, or after two seconds. Before, it waited for its own page to notice. A popout whose page had not loaded yet stayed open, and once loaded it pulled the window back out. Those leftover popouts also made popping the same window out again take about 20 seconds, because they held Chromium's cache lock on the popout's address.

- A window is out in at most one popout. Tearing a window out, or opening it in its own window from its menu, while an earlier popout of it is still on its way back closes the earlier one first. Different windows of a workspace can still each have their own popout.

- A popout torn out of a Studio window that closes mid-drag now stays where it is, focused. Before, it kept following the cursor.

- A fast drag out now pulls the window out every time. The release could reach the workspace before the word that the window had gone out. Depending on which side heard the release first, the window then showed both in its popout and on the desktop for a few seconds, or the popout flashed and closed. `minds:window-drag-ended` now says whether the drag was cancelled. On any release, the app keeps a popout that is out and says `released`, and the workspace takes that word even after its own release.

- The Electron log records tear-out steps, the end of each title-bar drag, and every popout close with its reason.

- The embed contract now says `minds:reattach-window`'s frame is in fractions of the workspace surface, which is what the chrome has always sent. It used to say fractions of the backdrop.
