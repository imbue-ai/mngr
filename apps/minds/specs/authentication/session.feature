Feature: Session lifetime and integrity
  A successful sign-in establishes a session carried by a signed cookie.

  @survives-restart
  Scenario: Sessions survive a desktop-client restart
    Given a signed-in user
    When the desktop client is stopped and started again
    And the user reloads the home page
    Then they are still signed in
    And they do not need a new login code

  @tampered-token
  Scenario: An altered session token is treated as signed out
    Given a signed-in user
    When their session token is modified in any way
    And they request a signed-in page
    Then they are treated as signed out

  @foreign-token
  Scenario: A session minted by a different installation is not accepted
    Given a session token created by a desktop client with a different data directory
    When it is presented to this desktop client
    Then the bearer is treated as signed out

  @expired-token
  Scenario: Sessions expire after 30 days
    Given a session token issued more than 30 days ago
    When it is presented
    Then the bearer is treated as signed out
