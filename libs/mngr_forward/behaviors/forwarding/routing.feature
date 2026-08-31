Feature: Host-header routing
  One listen port serves the bare origin and every agent origin, and the Host header decides which.
  A host of the form "[<service>.]agent-<hex>.localhost" -- where <hex> is a 32-character hexadecimal agent id, "127.0.0.1" is accepted in place of "localhost", and an optional port may follow -- names an agent origin.
  Every other host, including a near miss on that form, is served as the bare origin, and nothing is forwarded.

  @agent-default-origin
  Scenario Outline: A bare "agent-<hex>" origin resolves to the default service
    When a request arrives with Host "<host>"
    Then it resolves to that agent's default service

    Examples:
      | host                                                 |
      | agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.localhost      |
      | agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.localhost:8421 |
      | agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.127.0.0.1:8421 |

  @service-origin
  Scenario Outline: A "<label>.agent-<hex>" origin is that registered service
    When a request arrives with Host "<host>"
    Then it is handled as a request to the named service of that agent

    Examples:
      | host                                                       |
      | editor.agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.localhost      |
      | editor.agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.localhost:8421 |

  @deeper-labels-same-service
  Scenario: A label chain deeper than the service name stays with that service
    The last label before the coordinate is the service name; any labels before it are that service's own sub-origin space, for a multi-origin service.

    When a request arrives with Host "sub.editor.agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.localhost"
    Then it is handled as a request to the "editor" service of that agent

  @default-service-redirect
  Scenario: A top-level navigation to the bare agent origin moves to the default service's label origin
    Only HTML navigations move; every non-HTML request is served on the bare origin unchanged, so the grammar matches a share, where the bare agent domain is never served directly.

    Given a signed-in user
    And a known agent whose default service has its own origin label
    When a top-level HTML navigation arrives for the bare agent origin
    Then it is redirected to the default service's label origin, preserving the path

  @legacy-host-origin
  Scenario: A legacy host-coordinate origin only redirects a navigation to the agent origin
    A "[<service>.]host-<hex>.localhost" origin is the retired coordinate; the proxy never serves a backend on it.

    When a top-level HTML navigation arrives for a "host-<hex>" origin
    Then it is redirected to the agent-keyed origin
    And any other request to that origin is refused, not forwarded

  @other-hosts-are-bare-origin
  Scenario Outline: Every other host is served as the bare origin
    A near miss on the agent form -- an agent id of the wrong length, or a non-hex id -- is not an agent origin; only a full 32-hex agent id counts.

    When a request arrives with Host "<host>"
    Then it is served by the bare origin
    And nothing is forwarded to any backend

    Examples:
      | host                    |
      | localhost:8421          |
      | agent-0f3c.localhost    |
      | agent-nothex.localhost  |
      | foo.localhost           |
      | example.com             |
