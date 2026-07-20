Feature: Sign-in with a one-time login code
  At startup the desktop client mints a fresh one-time code and prints the
  login URL to its terminal. Opening that URL in a browser is the only way
  to establish a session in a browser that has none.

  Background:
    Given a running desktop client
    And its terminal printed a login URL with a fresh one-time code

  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    Given the user is not signed in
    When the user opens the login URL in a browser
    Then the browser lands on the home page "/"
    And the user is signed in
    And the one-time code is now spent

  @used-code
  Scenario: A spent code cannot sign anyone in again
    Given the login URL has already been used to sign in
    When anyone presents the same code for authentication again
    Then authentication is refused, explaining the code is invalid or already used
    And no session is established

  @unknown-code
  Scenario: A code the client never issued is refused
    Given the user is not signed in
    When they present a made-up code for authentication
    Then authentication is refused, explaining the code is invalid or already used
    And no session is established

  @prefetch
  Scenario: Fetching the login URL without executing scripts does not spend the code
    Given the user is not signed in
    When something fetches the login URL without executing its scripts (a link preloader, a chat-app unfurler, a browser prerenderer)
    Then the code remains unspent
    And the user can still sign in later by opening the same URL in a real browser

  @already-signed-in
  Scenario: Opening a login URL while already signed in does not spend the code
    Given the user is already signed in
    When they open a login URL carrying a fresh code
    Then they are redirected to the home page "/"
    And the code remains unspent

  @missing-code
  Scenario Outline: Sign-in requests without a code are malformed input, not server errors
    When a request is made to "<path>" with no one-time code parameter
    Then it is rejected as malformed input (HTTP 422)

    Examples:
      | path          |
      | /login        |
      | /authenticate |
