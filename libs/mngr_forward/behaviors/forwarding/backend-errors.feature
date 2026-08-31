Feature: When the backend cannot answer
  On the agent-origin HTTP path the proxy distinguishes backend failures by what the client can usefully do next, keyed on whether the failure struck before or after the backend's response headers arrived.
  A backend that never delivers response headers is a wait-and-retry condition (HTTP 503).
  A backend whose connection drops after its headers, partway through the body, is a lost response (HTTP 502).
  A backend that accepts but never answers -- or a proxy with no connection slot left to dial with -- is a gateway timeout (HTTP 504).

  Background:
    Given a running forward proxy
    And a signed-in user

  @unavailable-503
  Scenario Outline: A backend that never delivers response headers answers 503, shaped for the caller
    Before any response header arrives, a not-yet-known backend and one that cannot be reached are indistinguishable to the client -- each is a clean 503, and for a browser the styled loading page.

    Given a known agent whose backend <condition>
    When a request <accept> arrives for that agent origin
    Then the response is HTTP 503 <shape>

    Examples:
      | condition         | accept             | shape                             |
      | is not yet known  | accepting HTML     | with the loading page             |
      | is not yet known  | not accepting HTML | with a plain body and no redirect |
      | cannot be reached | accepting HTML     | with the loading page             |
      | cannot be reached | not accepting HTML | with a plain body and no redirect |

  @loading-page-self-heals
  Scenario: The loading page waits and enters the agent on its own
    Given a browser showing the loading page for an agent
    When that agent's backend starts answering
    Then the page notices on its own and loads the agent
    And the user never has to reload manually

  @mid-response-loss-502
  Scenario: A response lost after its headers yields 502
    Once the backend's response headers have arrived, a connection dropped partway through the body cannot fold back into a retry, so it resolves to a distinct lost-response status rather than the 503 loader.

    Given a known agent whose backend is routable
    When the backend's connection is lost after its headers but before its body is complete
    Then the client receives HTTP 502

  @wedged-backend-504
  Scenario: A backend that accepts but never answers yields 504
    Given a known agent whose backend is routable
    When the backend accepts a forwarded request but never sends a response
    Then the client eventually receives HTTP 504

  @stream-ends
  Scenario: An event stream that loses its backend simply ends
    Once a stream's status and headers have been delivered they cannot be revised, so a mid-stream loss cannot become an error status; the stream ends, and reconnecting is the client's concern.

    Given a client receiving an event stream through the proxy
    When the backend connection is lost mid-stream
    Then the stream ends
