Feature: When the backend cannot answer
  On the workspace-origin HTTP path the proxy distinguishes backend
  failures by what the client can usefully do next. A backend that does
  not exist yet or cannot be reached is a wait-and-retry condition
  (HTTP 503); a backend that accepted the connection but never answered
  within the proxy's timeout is a gateway timeout (HTTP 504); a response
  that was cut off after it began is a lost response (HTTP 502).

  Background:
    Given a running forward proxy
    And a signed-in user

  @unavailable-503
  Scenario Outline: An unavailable backend answers 503, shaped for the caller
    Given a known agent whose backend <condition>
    When a request <accept> arrives for that agent's workspace origin
    Then the response is HTTP 503 <shape>

    Examples:
      | condition          | accept              | shape                             |
      | is not yet known   | accepting HTML      | with the "Loading workspace" page |
      | is not yet known   | not accepting HTML  | with a plain body and no redirect |
      | cannot be reached  | accepting HTML      | with the "Loading workspace" page |
      | cannot be reached  | not accepting HTML  | with a plain body and no redirect |

  @loading-page-self-heals
  Scenario: The loading page waits and enters the workspace by itself
    Given a browser showing the "Loading workspace" page for an agent
    When the agent's backend starts answering
    Then the page notices on its own and loads the workspace
    And the user never has to reload manually

  @wedged-backend
  Scenario: A backend that accepts but never answers yields 504
    Given a known agent whose backend is routable
    When the backend accepts a forwarded request but does not answer within the proxy's timeout
    Then the client receives HTTP 504

  @mid-response-loss
  Scenario: A response lost partway through yields 502
    For ordinary (non-stream) requests the proxy relays the response only
    once it has arrived in full, so a backend that dies partway through
    producing one still results in a clean error status.

    Given a known agent whose backend is routable
    When the backend's connection is lost partway through its response to a forwarded request
    Then the client receives HTTP 502

  @stream-ends
  Scenario: An event stream that loses its backend simply ends
    Once a stream's status and headers have been delivered they cannot be
    revised, so a mid-stream loss cannot become an error status; the stream
    ends and reconnecting is the client's concern.

    Given a client receiving an event stream through the proxy
    When the backend connection is lost mid-stream
    Then the stream ends
