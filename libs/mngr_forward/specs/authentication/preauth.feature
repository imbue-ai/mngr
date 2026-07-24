Feature: Pre-authorized sessions for an embedding host
  A host application that spawns the proxy may configure an opaque preauth
  cookie value at startup and pre-set that value in its own browser shell,
  so the shell's first navigation is already signed in and the one-time-code
  flow never runs.

  Background:
    Given a running forward proxy that was started with a preauth cookie value

  @preauth-accepted
  Scenario: Presenting the exact preauth value counts as signed in
    Given a browser whose session cookie is exactly the configured preauth value
    When it requests a signed-in page on the bare origin
    Then it is treated as signed in
    And no one-time code is spent

  @preauth-on-workspace-origins
  Scenario: The preauth value signs requests in on workspace origins too
    Given a browser whose session cookie is exactly the configured preauth value
    When it requests a page on a workspace origin
    Then the request is treated as signed in on that origin

  @preauth-near-miss
  Scenario: Anything but the exact value falls back to signature verification
    Given a browser whose session cookie differs from the preauth value in any way
    And that cookie is not a signed token issued by this proxy
    When it requests a signed-in page
    Then it is treated as signed out
