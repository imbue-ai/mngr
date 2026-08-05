Feature: Session authentication with a one-time code
  At startup the desktop client mints a fresh one-time code and prints the authentication URL to its terminal.
  Opening that URL in a browser is the only way to authenticate a session in a browser that has none.

  Background:
    Given a running desktop client
    And its terminal printed an authentication URL with a fresh one-time code

  @fresh-code
  Scenario: Opening a fresh authentication URL authenticates the session
    Given the user has no session
    When the user opens the authentication URL in a browser
    Then the browser lands on the home page "/"
    And the session is authenticated
    And the one-time code is now spent

  @used-code
  Scenario: A spent code cannot authenticate a session again
    Given the authentication URL has already been used to authenticate a session
    When anyone presents the same code for authentication again
    Then authentication is refused, explaining the code is invalid or already used
    And no session is established

  @unknown-code
  Scenario: A code the client never issued is refused
    Given the user has no session
    When they present a made-up code for authentication
    Then authentication is refused, explaining the code is invalid or already used
    And no session is established

  @prefetch
  Scenario: Fetching the authentication URL without executing scripts does not spend the code
    Given the user has no session
    When something fetches the authentication URL without executing its scripts
    Then the code remains unspent
    And the user can still authenticate later by opening the same URL in a real browser

  @already-authenticated
  Scenario: Opening an authentication URL while the session is already authenticated does not spend the code
    Given the user's session is already authenticated
    When they open an authentication URL carrying a fresh code
    Then they are redirected to the home page "/"
    And the code remains unspent

  @missing-code
  Scenario Outline: Authentication requests without a code are malformed input, not server errors
    When a request is made to "<path>" with no one-time code parameter
    Then it is rejected as malformed input (HTTP 422)

    Examples:
      | path          |
      | /login        |
      | /authenticate |
