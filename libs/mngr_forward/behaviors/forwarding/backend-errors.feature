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
    Before any response header arrives, a not-yet-known backend and one that cannot be reached share one status -- each is a clean 503, and for a browser the styled loading page; only the reason header (`unavailable-reason`) tells them apart.

    Given a known agent whose backend <condition>
    When a request <accept> arrives for that agent origin
    Then the response is HTTP 503 <shape>

    Examples:
      | condition         | accept             | shape                             |
      | is not yet known  | accepting HTML     | with the loading page             |
      | is not yet known  | not accepting HTML | with a plain body and no redirect |
      | cannot be reached | accepting HTML     | with the loading page             |
      | cannot be reached | not accepting HTML | with a plain body and no redirect |

  @unavailable-reason
  Scenario Outline: The proxy's own 503 names why no backend answered
    Given a signed-in request to an agent origin where <condition>
    When the request arrives
    Then the response is HTTP 503
    And its X-Mngr-Forward-Reason header is "<reason>"

    Examples:
      | condition                                                            | reason                  |
      | discovery has not reported yet                                       | DISCOVERY_PENDING       |
      | discovery has reported and holds no agent for the origin             | UNKNOWN_AGENT           |
      | the origin has no route and no stopped events stream explains it     | NO_ROUTE                |
      | the origin has no route and the agent's events stream is not running | NO_ROUTE_EVENTS_STOPPED |

  @request-sent-mark
  Scenario Outline: The proxy's own gateway answers say whether the request reached the backend
    A client may claim that nothing was changed only when the request never reached the backend.

    Given a known agent whose backend <failure>
    When a request arrives for that agent origin
    Then the response is HTTP <status>
    And its X-Mngr-Forward-Request-Sent header is "<sent>"

    Examples:
      | failure                                                     | status | sent |
      | has no route                                                | 503    | no   |
      | refuses the connection                                      | 503    | no   |
      | accepts the request and then resets before answering        | 503    | yes  |
      | resets after its response headers                           | 502    | yes  |
      | cannot be dialed before the dial times out                  | 504    | no   |
      | leaves the proxy no free connection slot                    | 504    | no   |
      | accepts the request and never answers within the read budget | 504    | yes  |

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
