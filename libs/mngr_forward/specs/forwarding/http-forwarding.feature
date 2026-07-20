Feature: HTTP byte-forwarding
  An authenticated request to a routable agent's workspace origin is passed
  through to the agent's backend, and the backend's answer is passed back,
  with as little interpretation as possible. The proxy adds behavior only
  at the security boundary (the session cookie, per the corpus invariants)
  and when no backend answer exists at all.

  Background:
    Given a running forward proxy
    And a signed-in user
    And a known agent whose backend is routable

  @request-preserved
  Scenario: The request reaches the backend as sent
    When the user's client sends a request to the agent's workspace origin
    Then the backend receives the same method, path, query string, headers, and body
    But the Host header names the backend rather than the workspace origin
    And the proxy's session cookie is not among the forwarded cookies

  @response-preserved
  Scenario: The backend's answer returns to the client as produced
    Transport framing headers (transfer-encoding, content-encoding,
    content-length) are re-derived by the proxy's own transport; everything
    else passes through.

    When the backend answers a forwarded request
    Then the client receives the backend's status code, headers, and body unchanged

  @redirects-not-followed
  Scenario: Backend redirects go to the client, not the proxy
    When the backend answers with a redirect
    Then the client receives that redirect itself
    And the proxy does not follow it

  @sse-streamed
  Scenario: Event streams flow incrementally
    When the user's client requests an event stream (accepting "text/event-stream")
    Then bytes from the backend are delivered to the client as they arrive
    And the proxy does not wait for the response to complete

  @errors-pass-through
  Rule: Backend responses are never reinterpreted
    A non-success status produced by the backend is the backend's answer,
    and the proxy forwards it unchanged -- it never replaces a backend
    error with a page of its own. The proxy's own error responses occur
    only when no backend answer exists.

    @backend-error-forwarded
    Example: A backend error page reaches the client as the backend produced it
      When the backend answers a forwarded request with an error status and body of its own
      Then the client receives exactly that status and body
