Feature: Home page routing
  "/account/<account_id>/" is an account's home page, and "/" redirects to the most recently shown account's, or, until an account has been shown, to the signed-in account's with the most workspaces here, else the device's.
  For an authenticated user, what it shows depends on the one-time consent gate, whether initial workspace discovery has finished, which of the account's workspaces are known, and -- when none are -- whether the installation has ever been taken past the first-run start flow ("onboarding complete": a workspace create was started, or the user signed in from the start flow's "I have an account (log in)" answer to an account that already has workspaces).

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
    Then they are taken to the start flow, the chat that creates their first workspace
    And once a create is started, or they sign in from the start flow's "I have an account (log in)" answer to an account that already has workspaces, no later visit to a home page ever hands over to the start flow again

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
