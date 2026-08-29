Feature: Detecting an out-of-date workspace
  The app reports a workspace as out of date only on a positive reading of both versions.
  An unknown workspace is offered a check, not an update it is assumed to need: the workspace's own update agent reads its upstream and decides what applies, and finding nothing is an ordinary outcome.
  A workspace newer than the app reports that the app is behind.
  A workspace older than the oldest release that can be updated in place is reported as needing recreation, with no update run sent in to find that out.

  @behind-the-app
  Scenario: A workspace on an older release is out of date
    Given the app supports a template release
    And a workspace running an earlier template release
    Then the app reports that workspace as out of date
    And the app offers to update that workspace

  @at-the-app
  Scenario: A workspace on the supported release is up to date
    Given the app supports a template release
    And a workspace running that same template release
    Then the app reports that workspace as up to date
    And the app offers no update for that workspace

  @ahead-of-the-app
  Scenario: A workspace on a newer release reports the app as behind
    Given the app supports a template release
    And a workspace running a later template release
    Then the app reports that the app is behind that workspace
    And the app offers no update for that workspace

  @too-old-to-update-in-place
  Scenario: A workspace below the in-place cutoff needs recreating
    The cutoff is a fixed release, so this is a fact about the workspace alone: a development build with no supported release of its own still reads it.
    Given a workspace running a template release older than minds-v0.3.10
    Then the app reports that workspace as needing recreation
    And the app offers no update for that workspace
    And the app explains that the user should create a new workspace and ask its agent to migrate the old one's work into it

  @no-workspace-version
  Scenario: A workspace with no readable template version is unknown
    Given the app supports a template release
    And a workspace whose template version cannot be read
    Then the app reports that workspace's version as unknown
    And the app offers to check that workspace for an update

  @development-build
  Scenario: A build with no supported version has no opinion about any workspace
    Given the app is a development build with no supported template release
    And a workspace running a template release
    Then the app reports that workspace's version as unknown
    And the app offers to check that workspace for an update

  @unknown-names-the-missing-side
  Scenario Outline: An unknown reading says which side has no version
    When neither side has one, the app's own is named, since it accounts for every workspace rather than this one.
    Given the app's supported release is "<app>"
    And a workspace whose template version reads "<workspace>"
    Then the app reports that workspace's version as unknown
    And the app attributes that to "<side>"

    Examples:
      | app           | workspace     | side          |
      | a branch      | minds-v0.3.9  | the app       |
      | minds-v0.4.1  | unreadable    | the workspace |
      | a branch      | unreadable    | the app       |

  @unknown-workspace-is-dispatchable
  Scenario: A workspace with no readable version may still be sent its update agent
    Given the app supports a template release
    And a workspace whose template version cannot be read
    When the user asks to update that workspace
    Then an update agent is started in that workspace

  @bulk-covers-only-confirmed-workspaces
  Scenario: An action covering several workspaces passes over the unknown ones
    Given a workspace confirmed to be out of date
    And a workspace whose template version cannot be read
    When the user updates every workspace with an update available
    Then only the workspace confirmed to be out of date is dispatched

  @version-sources
  Rule: A running workspace is read from what it is running, a stopped one from what it was created at
    The created-at version never changes and is never preferred over the running one, or a workspace that already updated itself would be reported as out of date forever.

    @updated-workspace-not-re-offered
    Example: A workspace that already updated itself is not offered the same update again
      Given a workspace created at an earlier template release
      And that workspace is running the supported template release
      Then the app reports that workspace as up to date
