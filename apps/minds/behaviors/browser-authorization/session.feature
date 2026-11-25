Feature: Session lifetime and integrity
  Authenticating a session with a one-time code establishes it, carried by a signed session cookie.
  A signed session cookie is accepted for 30 days from the moment it was issued, and every request that presents an accepted one is answered with a freshly issued one.
  A session therefore lasts for as long as it is used at least once every 30 days, and ends once it has gone unused for longer.

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
  Scenario: A session cookie issued more than 30 days ago is not accepted
    Given a session cookie issued more than 30 days ago
    When it is presented
    Then the bearer is treated as unauthenticated

  @renewed-in-use
  Scenario: A session in use is renewed
    Given a session cookie issued less than 30 days ago
    When it is presented on a request
    Then the response carries a freshly issued session cookie
    And the fresh cookie is accepted for 30 days from that request

  @expired-not-renewed
  Scenario: An expired session cookie is not renewed
    Given a session cookie issued more than 30 days ago
    When it is presented on a request
    Then the response carries no session cookie
