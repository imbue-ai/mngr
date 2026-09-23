Feature: HTTP byte-forwarding
  An authenticated request to a routable agent origin is passed through to that agent's backend, and the backend's answer is passed back, with as little interpretation as possible.
  The proxy adds behavior only at the security boundary and when no backend answer exists.

  Background:
    Given a running forward proxy
    And a signed-in user
    And a known agent whose backend is routable

  @request-preserved
  Scenario: The request reaches the backend as sent
    When the user's client sends a request to the agent origin
    Then the backend receives the same method, path, query string, and body
    And the Host header names the backend rather than the agent origin
    And the proxy's session cookie is not among the forwarded cookies

  @request-headers-stamped
  Scenario: The proxy stamps the host application's per-agent headers on every forwarded request
    An embedding host application may hand the proxy a request-headers file naming, per agent or for every agent, headers to set on the requests it forwards.
    Every header the file names anywhere is dropped from the inbound request before the agent's own values are set, so a backend page cannot forge one; headers the file does not name pass through untouched.

    Given a request-headers file that names headers for the agent
    When the user's client sends a request to the agent origin carrying its own copy of one of those headers
    Then the backend receives the headers the file prescribes for the agent
    And the client's copy of the named header was replaced, not passed through

  @response-preserved
  Scenario: A buffered backend answer returns to the client unchanged
    The proxy re-derives only the transport framing headers (transfer-encoding, content-encoding, content-length); the status, remaining headers, and body pass back untouched, duplicate headers included.
    This fidelity is for ordinary buffered responses; on an event stream the proxy owns the response framing, so a stream's headers do not pass through unchanged (see `sse-streamed`).

    When the backend answers a forwarded request that is not an event stream
    Then the client receives the backend's status code, remaining headers, and body unchanged

  @frame-ancestors-appended
  Scenario: The proxy appends its own framing policy to every agent response
    Embedding policy is the proxy's to set, so it appends a frame-ancestors content-security-policy to each agent response rather than altering what the backend sent; multiple such headers compose by intersection.

    When the backend answers a forwarded request
    Then the client's response carries the backend's own headers plus the proxy's appended frame-ancestors policy

  @redirects-not-followed
  Scenario: Backend redirects go to the client, not the proxy
    When the backend answers with a redirect
    Then the client receives that redirect itself
    And the proxy does not follow it

  @sse-streamed
  Scenario: Event streams flow incrementally
    When the user's client requests an event stream
    Then bytes from the backend are delivered to the client as they arrive
    And the proxy does not wait for the response to complete

  @errors-pass-through
  Rule: A backend's own response is never reinterpreted
    A non-success status the backend produces is the backend's answer, and the proxy forwards it unchanged.
    The proxy's own error responses occur only when no backend answer exists.

    @backend-error-forwarded
    Example: A backend error page reaches the client as the backend produced it
      When the backend answers a forwarded request with an error status and a body of its own
      Then the client receives exactly that status and body
