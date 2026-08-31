Feature: Sign-in with a one-time code
  The proxy prints one login URL to its own output at startup, carrying a single-use code.
  Opening that URL is the only interactive way to establish a session in a browser that has none.
  The URL lands on a page whose script navigates to the authentication endpoint; only that endpoint spends the code.

  Background:
    Given a running forward proxy

  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    Given the user is not signed in
    When they open the login URL in a browser
    Then the browser lands signed in on the bare-origin home page "/"

  @login-page-inert
  Scenario: Fetching the login URL spends no code
    The login URL renders a page whose only action is a script that navigates to the authentication endpoint, so fetching it without running that script leaves the code unspent.

    When the login URL is fetched without executing its page script
    Then no code is spent
    And the user is still signed out

  @authenticate-sets-session
  Scenario: Reaching the authentication endpoint with the code establishes the session
    When a browser reaches the authentication endpoint carrying the one-time code
    Then the code is spent
    And a session cookie is set
    And the browser is redirected to the bare-origin home page "/"

  @already-signed-in
  Scenario: Opening the login URL while already signed in just goes home
    Given a signed-in user
    When they open the login URL again
    Then they are redirected to the bare-origin home page "/"
    And no code is spent

  @bad-code-refused
  Scenario Outline: A missing or unusable code is refused without a session
    When a browser reaches the authentication endpoint <code>
    Then authentication is refused
    And no session is established

    Examples:
      | code                      |
      | carrying no one-time code |
      | carrying an unknown code  |
      | carrying a spent code     |
