Feature: Landing page routing
  "/" is the home page. What it shows depends on sign-in state, the one-time
  consent gate, and which workspaces are known.

  @signed-out-home
  Scenario: Signed-out visitors see the sign-in prompt
    Given the user is not signed in
    When they visit "/"
    Then they see a sign-in prompt directing them to the login URL printed in the terminal
    And the page reveals nothing about existing workspaces

  @consent-gate
  Scenario: The consent question is asked once, right after sign-in
    Given a signed-in user who has never answered the error-reporting consent question
    When they visit "/"
    Then they see the "Help improve Minds" consent screen instead of the landing content
    When they answer the consent question
    Then no later visit to "/" ever shows the consent screen again

  @discovering
  Scenario: While the first workspace discovery is still running, show progress
    Given a signed-in user who has answered the consent question
    And no workspaces are known yet
    And the initial workspace discovery has not finished
    When they visit "/"
    Then they see a "Discovering agents" progress page that refreshes itself

  @empty-shows-create-form
  Scenario: With no workspaces, the home page is the create form
    Given a signed-in user who has answered the consent question
    And the initial workspace discovery finished without finding any workspace
    When they visit "/"
    Then they see the new-workspace form

  @deep-link-prefill
  Scenario: A deep link pre-fills the create form
    Given a signed-in user who has answered the consent question
    And the initial workspace discovery finished without finding any workspace
    When they visit "/" with a git URL and/or branch in the query string
    Then the new-workspace form is pre-filled with those values
    And the form opens with its advanced fields visible

  @lists-workspaces
  Scenario: With workspaces, the home page lists every one of them
    Given a signed-in user who has answered the consent question
    And they have one or more workspaces (discovered locally or synced from their other devices)
    When they visit "/"
    Then every one of those workspaces is listed
