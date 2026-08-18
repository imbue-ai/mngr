Feature: Session lifetime and integrity
  Authenticating a session establishes it, carried by a signed session cookie.

  @survives-restart
  Scenario: Sessions survive a desktop-client restart
    Given an authenticated user
    When the desktop client is stopped and started again
    And the user reloads the home page
    Then they are still authenticated
    And they do not need a new one-time code

  @tampered-cookie
  Scenario: An altered session cookie is treated as unauthenticated
    Given an authenticated user
    When their session cookie's signed content is modified
    And they request a page that requires a session
    Then the bearer is treated as unauthenticated

  @foreign-cookie
  Scenario: A session cookie minted by a different installation is not accepted
    Given a session cookie created by a desktop client with a different data directory
    When it is presented to this desktop client
    Then the bearer is treated as unauthenticated

  @expired-cookie
  Scenario: Sessions expire after 30 days
    Given a session cookie issued more than 30 days ago
    When it is presented
    Then the bearer is treated as unauthenticated
