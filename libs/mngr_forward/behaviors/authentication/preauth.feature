Feature: Pre-authorized sessions for an embedding host
  A host application that spawns the proxy can arrange for its own browser shell to arrive already signed in, so the one-time-code flow never runs.
  It has two independent paths for this: a session cookie value pre-set on the bare origin, and a browser-bridge token redeemed at the bare origin.
  Both are secrets fixed when the proxy starts and matched by exact value; unlike the one-time code, neither is spent by use, so each stays valid for the life of the process.

  @preauth-accepted
  Scenario: Presenting the exact preauth value counts as signed in
    Given a forward proxy started with a preauth cookie value
    And a browser whose session cookie is exactly that value
    When it requests a signed-in page on the bare origin
    Then it is treated as signed in
    And no one-time code is spent

  @preauth-on-agent-origins
  Scenario: The preauth value signs requests in on agent origins too
    Given a forward proxy started with a preauth cookie value
    And a browser whose session cookie is exactly that value
    When it requests a page on an agent origin
    Then the request is treated as signed in on that origin

  @preauth-near-miss
  Scenario: Anything but the exact preauth value falls back to signature verification
    Given a forward proxy started with a preauth cookie value
    And a browser whose session cookie differs from that value in any way
    And that cookie is not a signed token issued by this proxy
    When it requests a signed-in page
    Then it is treated as signed out

  @browser-bridge-token
  Scenario: A valid browser-bridge token mints a bare-origin session without a code
    Given a forward proxy started with a browser-bridge token
    When a browser presents that exact token to the browser-bridge route
    Then it is signed in on the bare origin
    And no one-time code is spent
    And it is redirected to a same-origin destination

  @browser-bridge-forged
  Scenario: A browser-bridge token that does not match is refused
    Given a forward proxy started with a browser-bridge token
    When a browser presents a different token to the browser-bridge route
    Then it is refused with HTTP 403
    And no session is established

  @browser-bridge-absent
  Scenario: The browser-bridge route does not exist unless a token was configured
    Given a forward proxy started without a browser-bridge token
    When a browser requests the browser-bridge route
    Then the response is HTTP 404
