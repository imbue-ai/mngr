Feature: Reaching a host that is not running
  A stopped host keeps its persisted storage, and the host directory lives there, so files under the host directory remain addressable while the host is down.
  Everything this feature records follows from that one fact and from the host directory's boundary.

  @stopped-host-reads
  Scenario: A file under the host directory can be read while the host is stopped
    Given a stopped host whose persisted storage can be reached
    When a file under its host directory is read
    Then the file's content is returned
    And the host is still stopped afterwards

  @stopped-host-writes
  Scenario: A file written to a stopped host is there when it runs again
    Given a stopped host whose persisted storage can be reached
    When a file is written under its host directory
    Then the write is reported as having succeeded
    And reading that path back returns the content just written
    And the file is present when the host next runs

  @work-dir-unreachable-when-stopped
  Scenario: A work directory cannot be reached while its host is stopped
    A work directory lies outside the host directory, so no persisted storage holds it while the host is down.

    Given an agent on a stopped host
    When a subcommand is invoked with a relative path against that agent's work directory
    Then the command refuses and reports that the host is stopped
    And it names the bases that can be reached while the host is stopped

  @no-persisted-storage
  Scenario: A stopped host with unreachable storage is refused
    Given a stopped host whose persisted storage cannot be reached
    When any subcommand addresses a path on it
    Then the command refuses and reports that the host cannot be reached while stopped

  @mode-not-applied-when-stopped
  Scenario: Requested permissions are reported as not applied on a stopped host
    Permissions are a property of a live filesystem, so a write to persisted storage cannot carry them.

    Given a stopped host whose persisted storage can be reached
    When a file is written under its host directory with requested permissions
    Then the content is written
    And the user is told the requested permissions were not applied

  @reduced-detail-when-stopped
  Scenario: A listing from a stopped host reports only what its storage can tell
    Given a stopped host whose persisted storage can be reached
    When a directory under its host directory is listed
    Then every entry is reported as either a file or a directory
    And entries report no permissions
