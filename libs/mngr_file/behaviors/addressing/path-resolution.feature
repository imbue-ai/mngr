Feature: Resolving a path against a base
  A relative path is resolved against the base directory the target fixed; an absolute path names its file outright.
  Which base an agent target fixes is the user's explicit choice, and the default is the agent's work directory.

  @work-dir-default
  Scenario: A relative path against an agent means a file in its work directory
    Given an agent target
    When a subcommand is given a relative path and no base is requested
    Then the path is resolved against that agent's work directory

  @state-dir-base
  Scenario: The agent state directory can be requested as the base
    Given an agent target
    When a subcommand is given a relative path and the agent's state directory is requested as the base
    Then the path is resolved against that agent's state directory

  @host-dir-base
  Scenario: The host directory can be requested as the base for an agent target
    Given an agent target
    When a subcommand is given a relative path and the host directory is requested as the base
    Then the path is resolved against the host directory of the host that agent runs on

  @host-target-base
  Scenario: A host target always resolves against the host directory
    Given a host target
    When a subcommand is given a relative path
    Then the path is resolved against that host's host directory

  @absolute-path-wins
  Scenario Outline: An absolute path names its file regardless of the base
    An absolute path is the escape hatch from base selection: it reaches anywhere on the machine the target selected, including outside every directory mngr owns.

    Given a target of kind "<target kind>"
    And a file outside every base directory that target admits
    When a subcommand is given that file's absolute path
    Then it acts on that file
    And no base directory is consulted

    Examples:
      | target kind |
      | agent       |
      | host        |

  @state-base-needs-an-agent
  Scenario: Requesting an agent-only base for a host target is a usage error
    A host has no state directory of its own, so the request cannot be honored and is not quietly reinterpreted as the host directory.

    Given a host target
    When a subcommand is invoked requesting the agent state directory as the base
    Then the command refuses as a usage error
    And it reports that the requested base applies only to agent targets
