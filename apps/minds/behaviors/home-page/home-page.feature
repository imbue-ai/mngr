Feature: Home page routing
  "/" is the home page.
  For an authenticated user, what it shows depends on the one-time consent gate, whether initial machine discovery has finished, and which machines are known.

  @consent-gate
  Scenario: The consent question is asked once, right after a session is authenticated
    Given an authenticated user who has never answered the error-reporting consent question
    When they visit "/"
    Then they see the "Help improve Minds" consent screen instead of the home page's normal content
    When they answer the consent question
    Then no later visit to "/" ever shows the consent screen again

  @discovering
  Scenario: While the first machine discovery is still running, show progress
    Given an authenticated user who has answered the consent question
    And no machines are known yet
    And the initial machine discovery has not finished
    When they visit "/"
    Then they see a "Discovering machines" progress page that refreshes itself

  @empty-shows-create-form
  Scenario: With no machines, the home page is the new-machine form
    Given an authenticated user who has answered the consent question
    And the initial machine discovery finished without finding any machine
    When they visit "/"
    Then they see the new-machine form

  @deep-link-prefill
  Scenario: A deep link pre-fills the new-machine form
    Given an authenticated user who has answered the consent question
    And the initial machine discovery finished without finding any machine
    When they visit "/" with a git URL and/or branch in the query string
    Then the new-machine form is pre-filled with those values
    And the form opens with its advanced fields visible

  @lists-machines
  Scenario: With machines, the home page lists every one of them
    Given an authenticated user who has answered the consent question
    And they have one or more machines (discovered locally or synced from their other devices)
    When they visit "/"
    Then every one of those machines is listed
