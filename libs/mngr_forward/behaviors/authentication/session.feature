Feature: Session lifetime and integrity
  A session is carried by a signed `mngr_forward_session` cookie and is the sole credential the proxy checks on every origin.
  Its trust rests on a signing key persisted in the proxy's state directory, so sessions outlive the process while one-time codes do not.
  A signed cookie is accepted for thirty days from the moment it was signed, and every HTTP request that presents an accepted one is answered with a freshly signed one.
  A session therefore lasts for as long as it is used at least once every thirty days, and ends once it has gone unused for longer.

  @survives-restart
  Scenario: A session survives a proxy restart
    Given a signed-in browser
    When the proxy is stopped and started again from the same state directory
    Then the browser is still signed in
    And it did not have to sign in again

  @foreign-installation-rejected
  Scenario: A session minted under another state directory is not accepted
    Given a session cookie signed by a proxy started from a different state directory
    When it is presented to this proxy
    Then it is treated as signed out

  @tampered-cookie-rejected
  Scenario: Any alteration of the cookie invalidates it
    Given a signed-in browser
    When any byte of its session cookie is altered
    Then the altered cookie is treated as signed out

  @expired-cookie-rejected
  Scenario: A session cookie signed more than thirty days ago is not accepted
    Given a session cookie older than thirty days
    When it is presented to this proxy
    Then it is treated as signed out

  @renewed-in-use
  Scenario: A session in use is renewed
    Given a session cookie signed by this proxy less than thirty days ago
    When it is presented on an HTTP request to an origin the proxy serves
    Then the response carries a freshly signed session cookie scoped as the presented one was
    And the fresh cookie is accepted for thirty days from that request

  @expired-not-renewed
  Scenario: An expired session cookie is not renewed
    Given a session cookie older than thirty days
    When it is presented on an HTTP request
    Then the response carries no session cookie
