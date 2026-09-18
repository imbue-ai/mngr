Feature: Choosing the machine
  A target names either an agent or a host, and the choice decides which machine a subcommand acts on.
  An agent does not hold files of its own: it runs on a host, and addressing the agent addresses that host with the agent's directories in view.

  @agent-target
  Scenario: An agent target acts on the host that agent runs on
    Given an agent that mngr can find
    When a subcommand is invoked with that agent as the target
    Then it acts on the host that agent runs on
    And the agent's own directories are available as bases for a relative path

  @host-target
  Scenario: A host target acts on that host
    Given a host that mngr can find
    When a subcommand is invoked with that host as the target
    Then it acts on that host
    And the only base available for a relative path is the host directory

  @unresolvable-target
  Scenario: A target matching nothing is refused before anything is addressed
    When a subcommand is invoked with a target that matches no agent and no host
    Then the command refuses and names the target it could not resolve
    And no file on any machine is read or written

  @ambiguous-target
  Scenario: A target matching more than one candidate is refused rather than guessed
    Given a target that matches more than one agent or more than one host
    When a subcommand is invoked with that target
    Then the command refuses and reports that the target is ambiguous
    And no file on any machine is read or written
