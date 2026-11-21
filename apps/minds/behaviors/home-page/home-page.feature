Feature: Home page routing
  "/account/<account_id>/" is an account's home page, and "/" redirects to the most recently shown account's, or, until an account has been shown, to the signed-in account's with the most workspaces here, else the device's.
  For an authenticated user, what it shows depends on the one-time consent gate, whether initial workspace discovery has finished, which of the account's workspaces are known, and -- when none are -- whether the installation has ever been taken past the first-run start flow ("onboarding complete": a workspace create was started, or the user signed in from the start flow to an account that already has workspaces).

  @bare-root-redirects
  Scenario Outline: "/" is the home page of the account most recently shown in a window, else of the signed-in account with the most workspaces
    Given an authenticated user who has answered the consent question
    And <history>
    When they visit "/"
    Then they land on <destination>

    Examples:
      | history                                                                                                                            | destination                                        |
      | no account has been shown in a window on this installation, and no signed-in account has workspaces on it                         | the device's home page, "/account/local/"          |
      | no account has been shown in a window on this installation, and "alice@example.com" is the signed-in account with most workspaces | that account's home page, "/account/<alice's id>/" |
      | the account "alice@example.com" was the last shown in a window                                                                     | that account's home page, "/account/<alice's id>/" |

  @consent-gate
  Scenario: The consent question is asked once, right after a session is authenticated
    Given an authenticated user who has never answered the error-reporting consent question
    When they visit a home page
    Then they see the "Help improve Imbue Studio" consent screen instead of the home page's normal content
    When they answer the consent question
    Then no later visit to a home page ever shows the consent screen again

  @consent-reporting-choice
  Scenario: The consent question is a checkbox that starts checked, and its answer is the reporting setting
    Given an authenticated user on the consent screen
    Then its one question, "Is it ok if we report data to help improve Studio?", is a checkbox that starts checked
    When they continue without unchecking it
    Then error reporting stays on
    When a user unchecks it and continues instead
    Then error reporting is turned off, as Settings -> Error reporting then shows

  @discovering
  Scenario: While the first workspace discovery is still running, show progress
    Given an authenticated user who has answered the consent question
    And none of the account's workspaces are known yet
    And the initial workspace discovery has not finished
    When they visit the account's home page
    Then they see a "Discovering workspaces" progress page that refreshes itself

  @empty-shows-create-form
  Scenario: With no workspaces and onboarding complete, the home page is the new-workspace form
    Given an authenticated user who has answered the consent question
    And the installation's onboarding is complete
    And the initial workspace discovery finished without finding any workspace of the account
    When they visit the account's home page
    Then they see the new-workspace form, which creates under that account

  @offers-accounts-with-workspaces
  Scenario: A home page with no workspaces of its own offers the accounts that have some
    Given an authenticated user who has answered the consent question
    And the initial workspace discovery finished without finding any workspace of the device
    And the account "alice@example.com" has workspaces on this installation
    When they visit the device's home page
    Then above the page's usual content it offers Alice's account, naming how many workspaces it has
    When they choose it
    Then the window moves to Alice's account as if they had chosen it in the account switcher

  @signed-out-account-offers-login
  Scenario: The home page of a signed-out account offers to log in instead of creating
    Given an authenticated user who has answered the consent question
    And the account "alice@example.com" is signed out but has workspaces on this installation
    When they visit that account's home page
    Then its workspaces are listed
    And where the page would offer to create a workspace it offers to log in

  @empty-starts-onboarding
  Scenario: With no workspaces and onboarding incomplete, the home page hands over to the start flow
    Given an authenticated user whose installation has never been taken past the start flow
    And the initial workspace discovery finished without finding any workspace
    When they visit a home page
    Then they are taken to the start flow, the sign-in page that creates their first workspace
    And once a create is started, or they sign in from the start flow to an account that already has workspaces, no later visit to a home page ever hands over to the start flow again

  @deep-link-prefill
  Scenario: A deep link pre-fills the new-workspace form
    Given an authenticated user who has answered the consent question
    And the initial workspace discovery finished without finding any workspace of the account
    When they visit the account's home page with a git URL and/or branch in the query string
    Then the new-workspace form is pre-filled with those values
    And the form opens with its advanced fields visible

  @lists-workspaces
  Scenario: With workspaces, the home page lists every one of the account's
    Given an authenticated user who has answered the consent question
    And the account has one or more workspaces (discovered locally or synced from their other devices)
    And another account has workspaces on this installation
    When they visit the account's home page
    Then every one of the account's workspaces is listed
    And none of the other account's workspaces is

  @home-tabs
  Scenario: The home page is three tabs, the third only while an invitation waits or was declined
    Given an authenticated user who has answered the consent question
    When they visit the account's home page
    Then a tab strip offers Workspaces and Apps, opening on Workspaces
    And an Invitations tab joins the strip, counting the shares still waiting for an answer, only while any listed share is unanswered or declined
    When they choose a tab
    Then the window comes back to that tab on its next visit to a home page

  @workspace-cards
  Scenario: Each workspace is a card painted with its wallpaper, with one run control and its statuses as badges
    Given an authenticated user who has answered the consent question
    And the account has workspaces, one of which has been loaded on this installation
    When they visit the account's home page on the Workspaces tab
    Then every workspace of the account is a card under "Your workspaces", ending in an "Add workspace" card (or "Sign in" on a signed-out account's hub), with the "Recently destroyed workspaces" link under the grid
    And the loaded workspace's card is painted with the wallpaper its desktop wears, and every other with its accent
    And a card carries its name, one run control (Start, Stop, or Restart), "Open in new window", and a menu with where it runs, Backups, and Workspace settings
    And every status that applies is a badge: red for what is broken, orange for what needs the user, a white chip for information and progress

  @shared-workspace-cards
  Scenario: A workspace shared with the account is a guest card under "Workspaces shared with you"
    Given an authenticated user who has answered the consent question
    And another person's workspace is shared with the account and has been accepted
    When they visit the account's home page on the Workspaces tab
    Then the share is a card under "Workspaces shared with you" naming its owner, with no run control
    And its liveness and the last open's verdict are badges, and open, retry, decline, report, and remove are in its menu
    And while an open runs the card reads "Connecting…", and the workspace surface shows the workspace connecting until its page is up, never a sign-in form
    And a link pasted into the section's input whose label is the workspace shell's becomes such a card reading "Pasted link"

  @apps-tab
  Scenario: The Apps tab lists the account's own apps, then the apps shared with it, as desktop icons
    Given an authenticated user who has answered the consent question
    And the account has a live workspace that registered an app
    And another person shared one app of their workspace with the account
    When they visit the account's home page on the Apps tab
    Then the workspace's app is an icon under "Your apps", wearing its registered icon (or a monogram of its name) and the workspace's name
    And choosing it opens the workspace on that app
    And the shared app is an icon under "Apps shared with you", wearing a monogram, the sharer's face, and a "From" tooltip
    And an app of a shared workspace that its owner granted in its own right is an icon there too, beside the workspace's card
    And under the shared apps an input takes an app link they were given, in place of an empty state
    And system apps (chat, terminal, files, routines) are listed under neither

  @invitations-tab
  Scenario: A share not yet answered is an invitation to join or decline
    Given an authenticated user who has answered the consent question
    And another person shared a workspace with the account that the account has not answered
    When they visit the account's home page on the Invitations tab
    Then the share is a card naming who invited them to a workspace or an app and when, with Decline and Join
    When they choose Join
    Then the share opens as a card's click would, which accepts it
    When they choose Decline instead
    Then the card stays in place reading "Declined" with Undo until they leave the tab, after which it files under "Declined invitations"

  @settings-and-account
  Scenario: Studio settings sits in the section head, and the account page is reached from the account switcher
    Given an authenticated user who has answered the consent question
    When they visit a home page
    Then "Studio settings" is at the right of the active tab's first section head
    And the account switcher's menu ends with "Account settings", under a line, which opens the account page under the window's account
