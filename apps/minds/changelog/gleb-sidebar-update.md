The sidebar is now a floating menu: dark panel with rounded corners,
shadow, and a colored dot per workspace, matching the Figma "Space switcher
menu" design. In Electron the page loads into the shared modal
WebContentsView (transparent background), so the panel reads as a floating
overlay above the workspace content. Each row's accent is shown by the dot
alone -- the old left-edge vertical accent stripe (carried over from the
docked sidebar) is removed as redundant.

Every workspace row reveals its per-workspace settings gear on hover (and
in Electron, an "Open in new window" button alongside it); the current
workspace's row shows those icons at all times. Two new rows at the bottom
of the menu: "New workspace"
(navigates to /create) and "Manage account(s)" / "Log in" (replaces the
account button that used to sit in the titlebar). The titlebar no longer
shows the account button.

The sidebar behaves like a modal: clicking anywhere outside the menu (or
pressing Escape) closes it. The menu's height comes from its own flex
layout -- no JS measurement or per-bundle bounds math.

Each window now hosts three WebContentsView surfaces instead of four:
chrome (titlebar), content (workspace), and a single shared overlay used
by both the sidebar and the inbox. The sidebar URL (/_chrome/sidebar) is
loaded into the same modalView that hosts /inbox, so dismissal,
titlebar-drag suppression, transparent background, and Escape handling
all come from the existing modal infrastructure.

The menu's position is now driven entirely by the call site, not by an
inferred ``is_mac`` flag. The chrome page reads the trigger button's
``getBoundingClientRect`` and passes the rect + a caller-chosen offset
through; the menu anchors at ``trigger.bottom-left + offset`` regardless
of where the trigger lives. In Electron that goes over IPC into
``/_chrome/sidebar``'s query string (``trigger_x`` / ``trigger_y`` /
``trigger_w`` / ``trigger_h`` / ``offset_x`` / ``offset_y``); in browser
mode chrome.js sets the inline panel's ``style.left`` / ``style.top``
directly. The panel uses ``py-1.5`` (vertical padding only) so the
row's ``px-2`` lines up exactly with the trigger button's icon offset
inside its ``w-8`` shell -- icon columns line up automatically. Moving
or restyling the trigger button in the future requires no template
changes.

An incoming permission request no longer yanks the open menu away. Now
that the sidebar and the inbox share one overlay view, auto-opening the
inbox is gated on no modal already being visible (previously it only
checked whether the *inbox* was open, so it would load the inbox over an
open sidebar). When a menu is up, the request surfaces via the live
titlebar badge instead, and auto-opens once the menu is dismissed and
the next request arrives.

On macOS the titlebar's left padding grew from 72px to 76px so the first
titlebar button's hover highlight clears the window's traffic lights with
a little more breathing room. The workspace menu follows automatically
(it anchors to that button's measured position), so no menu-side change
was needed.

The menu's internal spacing was tightened to a uniform grid: 4px padding
on all four sides of the panel, 2px between every entry, and 2px above
and below the divider line (the line is now a bare full-width rule that
takes its spacing from the panel's row gap rather than its own padding).

The menu is anchored 2px left of and 2px below the trigger button's
bottom-left corner (anchor offset (-2, 2)). Its background is a flat pure
black for now (was the dark-teal #0b292b) while the color treatment is
being iterated on.

The workspace row is now a single shared builder
(window.mindsSidebarRow.buildRow) rather than markup duplicated across
the Electron menu (sidebar.js) and the browser menu (chrome.js). The row
carries no outer positioning -- spacing is the parent container's flex
gap -- so it composes cleanly wherever it's dropped in. The styleguide's
"Sidebar items" sample renders through that same builder, so the catalog
can't drift from the live menu.
