Feature: Host-header routing
  One listen port serves two kinds of origin, and the Host header decides
  which. A host of the form "agent-<id>.localhost" -- where <id> is a
  well-formed 32-hex-character agent id, with an optional port, and with
  "127.0.0.1" accepted as a synonym for "localhost" -- names a workspace
  origin for that agent. Every other host, including near misses on the
  agent form, is served as the bare origin and nothing is forwarded.

  @workspace-host-forms
  Scenario Outline: Hosts naming a well-formed agent id are workspace origins
    When a request arrives with Host "<host>"
    Then it is handled as a workspace-origin request for the agent named in the host

    Examples:
      | host                                                  |
      | agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.localhost      |
      | agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.localhost:8421 |
      | agent-2f6c0d9c41f24d47a89f6f2f61b3a8d1.127.0.0.1:8421 |

  @other-hosts-are-bare-origin
  Scenario Outline: Every other host is served as the bare origin
    A near miss on the agent form -- a hex id of the wrong length, a
    non-hex id -- is not a workspace origin; only a well-formed agent id
    counts.

    When a request arrives with Host "<host>"
    Then it is served by the bare origin
    And nothing is forwarded to any backend

    Examples:
      | host                       |
      | localhost:8421             |
      | agent-0f3c.localhost       |
      | agent-workspace.localhost  |
      | foo.localhost              |
      | example.com                |
