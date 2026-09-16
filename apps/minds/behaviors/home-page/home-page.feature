Feature: Home page routing
  "/" is the home page.
  For an authenticated user, what it shows depends on the one-time consent gate, whether initial workspace discovery has finished, which workspaces are known, and -- when none are -- whether the installation has ever been taken past the first-run start flow ("onboarding complete": a workspace create was started, or the user signed in from the start flow's "I already have one" answer).

  @consent-gate
  Scenario: The consent question is asked once, right after a session is authenticated
    Given an authenticated user who has never answered the error-reporting consent question
    When they visit "/"
    Then they see the "Help improve Mind" consent screen instead of the home page's normal content
    When they answer the consent question
    Then no later visit to "/" ever shows the consent screen again

  @discovering
  Scenario: While the first workspace discovery is still running, show progress
    Given an authenticated user who has answered the consent question
    And no workspaces are known yet
    And the initial workspace discovery has not finished
    When they visit "/"
    Then they see a "Discovering workspaces" progress page that refreshes itself

  @empty-shows-create-form
  Scenario: With no workspaces and onboarding complete, the home page is the new-workspace form
    Given an authenticated user who has answered the consent question
    And the installation's onboarding is complete
    And the initial workspace discovery finished without finding any workspace
    When they visit "/"
    Then they see the new-workspace form

  @empty-starts-onboarding
  Scenario: With no workspaces and onboarding incomplete, the home page hands over to the start flow
    Given an authenticated user whose installation has never been taken past the start flow
    And the initial workspace discovery finished without finding any workspace
    When they visit "/"
    Then they are taken to the start flow, the chat that creates their first workspace
    And once a create is started, or they sign in from the start flow's "I already have one" answer, no later visit to "/" ever hands over to the start flow again

  @deep-link-prefill
  Scenario: A deep link pre-fills the new-workspace form
    Given an authenticated user who has answered the consent question
    And the initial workspace discovery finished without finding any workspace
    When they visit "/" with a git URL and/or branch in the query string
    Then the new-workspace form is pre-filled with those values
    And the form opens with its advanced fields visible

  @lists-workspaces
  Scenario: With workspaces, the home page lists every one of them
    Given an authenticated user who has answered the consent question
    And they have one or more workspaces (discovered locally or synced from their other devices)
    When they visit "/"
    Then every one of those workspaces is listed
