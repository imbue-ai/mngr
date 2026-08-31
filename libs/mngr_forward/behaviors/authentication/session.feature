Feature: Session lifetime and integrity
  A session is carried by a signed `mngr_forward_session` cookie and is the sole credential the proxy checks on every origin.
  Its trust rests on a signing key persisted in the proxy's state directory, so sessions outlive the process while one-time codes do not.

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
  Scenario: A session older than thirty days is not accepted
    Given a session cookie older than thirty days
    When it is presented to this proxy
    Then it is treated as signed out
