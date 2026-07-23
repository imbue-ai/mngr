# Plan: Titlebar simplification and centered modals

## Refined prompt

I want to make the following UI change...

CURRENTLY:

* The top left has home button, arrows, and more.

THIS CHANGE:

* Just a home button, with the added "Minds" name.
* Other screens that used to have arrows will get mostly changed into modals (i.e., settings, account management, logins).
* Modals should be centered (instead of on the left, like permission requests currently do).
* Use https://imbue-ai.github.io/mind-sketches/prototypes/minds-options/ as the design reference — it has the proper mockup of the top bar.
* The hamburger/sidebar menu goes away: on the home screen the top-left is just "⌂ Minds"; inside a workspace the top bar is "⌂ Minds / [workspace name] ⌄" followed by Workspace, Connections (with pending-request badge), and Workspace Settings icon-tabs.
* Clicking the workspace name opens a real workspace-switcher dropdown (stubbed in the prototype): the workspace list plus "New workspace" and the login/account entry.
* The workspace-switcher dropdown keeps the current menu's richness: grouped by account, hover actions (open in new window, workspace settings), and the native right-click context menu.
* Global back/forward arrows are removed; full-page flows that still need one (e.g. create workspace) get a contextual back arrow in the titlebar shown only on those pages.
* Permission requests move out of the left popup/drawer into the workspace's Connections view ("Waiting on you" list with Approve/Deny), per the sketch.
* Complex permission requests (file sharing, account selection, manual credentials) are handled in the Connections tab itself, which highlights the relevant permission request.
* The Connections view also absorbs connectors/permissions management from app-level Settings; the Minds Settings modal keeps only app-level items.
* New permission requests just update the Connections badge and send an OS notification (no auto-open); clicking the notification opens that workspace's Connections view.
* Settings / account management / login modals launch from the home screen's bottom-left entries ("Minds Settings", account email), matching the mockup.
* The top-right is reduced to just the bug-report icon (no "Jump to..." search, no help/requests toggles in the top bar), and it opens the existing help/bug-report centered modal unchanged.
* The Minds Settings modal matches the mockup: Appearance (dark mode promoted to a real user setting), Error reporting, Account default region (new setting), and About/version — plus the existing backup-password section as an extra section.
* The default-region setting is functional: it pre-selects the region when creating Imbue Cloud workspaces.
* Non-workspace full pages (create, consent, welcome) use breadcrumb titles ("⌂ Minds / [page name]") plus the contextual back arrow; the centered page title is dropped everywhere.
* No pending-request indicator on the home screen; OS notifications plus the in-workspace badge are enough.
* Browser mode (non-Electron) keeps full-page fallbacks for settings/accounts/login.
* Out of scope: sharing (no Share icon-tab, keep current entry points) and the workspace-internal chat/terminal tab strip.

## Overview

- The titlebar is simplified to match the minds-options mockup: the left side becomes a single "⌂ Minds" home button that grows into a breadcrumb ("⌂ Minds / workspace-name ⌄") inside a workspace, and the right side keeps only the bug-report button. The hamburger menu, back/forward arrows, requests toggle, and centered page title all leave the titlebar.

- Navigation moves from screen-hopping to overlays and tabs: app-level destinations (Minds settings, account management, login) become centered modals on the existing warm overlay surface, launched from new bottom-left entries on the home screen; workspace-level destinations become titlebar icon-tabs (Workspace, Connections, Workspace Settings) that switch what the content area shows.

- The left-anchored inbox drawer is retired. Pending permission requests move into a new per-workspace Connections view ("Waiting on you" with Approve/Deny), which also absorbs the connectors/permissions management that currently lives in app-level Settings. All remaining overlays follow the established centered-modal convention.

- The workspace switcher becomes a dropdown anchored to the workspace name in the breadcrumb, keeping everything the current hamburger menu offers (accounts grouping, hover actions, right-click menu, New workspace, account/login entry) except the Settings entry, which now lives on the home screen.

- Two settings get promoted to real user-facing settings in the Minds Settings modal: dark mode (currently a dev-only styleguide toggle) and default region (currently a last-used-value preference that is only written back by the create flow).

## Expected behavior

### Titlebar

- On the home screen the titlebar shows, after the macOS traffic lights: a single home button rendered as the home icon plus the label "Minds". The right side shows only the bug-report icon button. There is no hamburger, no back/forward arrows, no requests button, no search field, and no centered page title.
- Inside a workspace the left side reads "⌂ Minds / workspace-name ⌄" followed by three icon-tabs: Workspace, Connections (with a pending-request count badge), and Workspace Settings. The icon-tab for the currently visible view is highlighted. The workspace accent theming of the titlebar is unchanged.
- Clicking "⌂ Minds" always navigates to the home screen (workspace grid).
- Clicking "workspace-name ⌄" opens the workspace-switcher dropdown anchored under the name. It contains the same content as today's hamburger menu minus the Settings entry: workspaces grouped by account (with per-row hover actions "Open in new window" and "Workspace settings", and the native right-click context menu), "New workspace", and the account entry ("Manage account(s)" when signed in, "Log in" when signed out).
- Selecting a workspace in the dropdown behaves as today: focuses the existing window if that workspace is already open elsewhere, otherwise navigates the current window to it.
- On non-workspace full pages that remain (create workspace, creating/destroying progress, consent, welcome, recovery), the breadcrumb reads "⌂ Minds / page-name" and, where the page has a natural place to go back to (e.g. the create form), a contextual back arrow appears in the titlebar for that page only. Gate pages (welcome, consent) show no back arrow.
- Back/forward history navigation buttons are gone everywhere. The bug-report button opens the existing help/bug-report centered modal, unchanged.

### Home screen and centered modals

- The home screen gains two bottom-left entries, matching the mockup: "Minds Settings" (gear icon) and the signed-in account email with a "(+N)" suffix when more than one account is signed in (or a "Log in" entry when signed out).
- "Minds Settings" opens a centered modal with sections: Appearance (dark mode toggle — now a real persisted user setting that applies to the whole minds UI), Error reporting (existing setting, unchanged semantics), Account (default region dropdown), Backup password (existing section, moved over from the old settings page), and About (app version).
- The default-region setting is functional: the region chosen there pre-selects the region field when creating Imbue Cloud workspaces. The create flow continues to write back the last-used region, and the settings modal edits that same preference directly.
- The account entry opens the centered Manage Accounts modal: each signed-in account is listed with its provider and a "Default" badge, with per-account "Set default" and "Log out" actions, plus an "Add account" action.
- "Add account" (and "Log in" when signed out) opens the centered sign-in/create-account modal: email/password form, Google and GitHub OAuth buttons, and a toggle between sign-in and create-account. This follows the existing sign-in modal's flows.
- All of these modals are centered with a dimmed backdrop and close via Escape, backdrop click, or an X button. They open over whatever window they were launched from.
- In browser mode (non-Electron) the settings, accounts, and login destinations keep working as full-page navigations, as today.

### Connections view (replaces the inbox drawer)

- Each workspace has a Connections view, opened via its titlebar icon-tab. It shows two sections: "Waiting on you" — that workspace's pending permission requests as cards with a short description and Approve/Deny actions — and the connected services / granted permissions that workspace holds, with revoke actions (this content moves out of app-level Settings).
- Complex permission requests (file-sharing paths, account selection, manual credential entry) render their full forms within the Connections view; when the view is opened targeting a specific request (e.g. from a notification), that request is highlighted.
- The Connections icon-tab badge shows the count of that workspace's pending requests, updated live.
- When a new permission request arrives, nothing auto-opens: the badge updates and an OS notification is shown. Clicking the notification opens (or focuses) that workspace and shows its Connections view with the request highlighted.
- The left-anchored inbox drawer, its titlebar toggle, and the auto-open-on-new-request behavior are removed.

### Interactions of new and existing functionality

- The old full-page destinations reachable from the removed hamburger menu remain reachable: settings and accounts via the home screen (modals in Electron, pages in browser mode), workspace settings via the icon-tab and the dropdown's per-row hover action, new-workspace via the dropdown and the home screen's existing "+ New workspace".
- App-level Settings no longer contains connectors/permissions management; users manage those per workspace in Connections. The "revoke across all workspaces" bulk actions from the old settings page are dropped in favor of per-workspace revocation.
- Existing deep links and notification click-throughs that previously opened the inbox now land on the owning workspace's Connections view with the target request selected/highlighted.
- The dark-mode toggle replaces the dev-only styleguide toggle as the single source of truth for the theme; the workspace-internal UI (rendered by each workspace's own system interface) is unaffected by this change.

## Changes

- Titlebar: remove the hamburger, back/forward arrows, requests toggle, and centered page title; add the "Minds" label to the home button, the workspace/page breadcrumb, the three workspace icon-tabs with active-state highlighting and a pending-requests badge, and a per-page contextual back arrow.
- Workspace switcher: re-anchor the existing sidebar menu content (minus the Settings entry) as a dropdown under the breadcrumb's workspace name; remove the standalone sidebar surface and its browser-mode inline variant.
- Home screen: add the bottom-left "Minds Settings" and account/login entries as modal launchers.
- Centered modals: add Minds Settings, Manage Accounts, and sign-in/create-account modals on the existing overlay surface, following the established centered-modal convention; the existing full pages stay as browser-mode fallbacks and their content is updated to match (settings page loses connectors, gains appearance/region).
- Minds Settings content: drop the connectors/permissions section; add Appearance (promote the dev-only dark-mode toggle to a persisted user setting), Account default region (expose the existing per-provider region preference for editing), and About/version; keep error reporting and backup password.
- Connections view: add a per-workspace view combining the workspace's pending permission requests (with full request forms and highlight-on-target) and its granted connectors/permissions with revoke actions; reuse the existing permission-request fragments and grant/deny endpoints.
- Inbox retirement: remove the left drawer, its routes/toggles, and auto-open behavior; repoint notification click-throughs and request deep links at the owning workspace's Connections view.
- Notifications/badging: badge counts become per-workspace on the Connections icon-tab; OS notifications remain the only unprompted signal for new requests.
- Non-workspace pages: switch their titlebar presentation to breadcrumb + contextual back arrow; remove in-page "← Back to workspaces" links where the titlebar now covers the exit.
- Tests and docs: update titlebar/navigation/modal tests and visual baselines to the new structure; add coverage for the new modals, Connections view, switcher dropdown, and region/dark-mode settings; update user-facing docs and add the changelog entry.
