Feature: Detecting an out-of-date workspace
  The app reports a workspace as out of date only on a positive reading of both versions, and only when the workspace is on an earlier minor release than the app supports.
  A workspace behind the app within the minor release the app supports has a patch available: the app never prompts the user about it, but the user may still update it or schedule its update by hand.
  A workspace below the security floor has a security update when the app supports a release at or above that floor: the app prompts the user about it however small the gap, says that the update includes a critical security fix, and explains what that fix addresses when asked.
  An unknown workspace is offered a check, not an update it is assumed to need: the workspace's own update agent reads its upstream and decides what applies, and finding nothing is an ordinary outcome.
  A workspace newer than the app reports that the app is behind.
  A workspace older than the oldest release that can be updated in place is reported as needing recreation, with no update run sent in to find that out.

  @behind-the-app
  Scenario: A workspace on an earlier minor release is out of date
    Given the app supports a template release
    And a workspace running a template release from an earlier minor release
    Then the app reports that workspace as out of date
    And the app offers to update that workspace

  @minor-release-granularity
  Scenario Outline: Only a newer minor release makes a workspace out of date
    A prerelease belongs to the minor release it precedes.
    In every row either the workspace is at or above the security floor or the app's supported release is below it, so the floor decides nothing here.
    Given the app's supported release is "<app>"
    And a workspace whose template version reads "<workspace>"
    Then the app reports that workspace as "<reading>"

    Examples:
      | app           | workspace          | reading         |
      | minds-v0.10.0 | minds-v0.9.3       | out of date     |
      | minds-v0.8.4  | minds-v0.7.9       | out of date     |
      | minds-v1.0.0  | minds-v0.9.5       | out of date     |
      | minds-v0.8.4  | minds-v0.8.3       | patch available |
      | minds-v0.8.4  | minds-v0.8.0       | patch available |
      | minds-v0.10.0 | minds-v0.10.0-rc.1 | patch available |

  @below-the-security-floor
  Scenario Outline: A workspace below the security floor has a security update when the app can deliver it
    The security floor is minds-v0.9.0, the oldest template release with no known security issue.
    The floor outranks the minor-release rule: a workspace below it is offered a security update even when it is behind the app by only a patch or a prerelease.
    An app whose own supported release is below the floor cannot deliver the fix, so it reads such a workspace by the minor-release rule alone.
    Given the app's supported release is "<app>"
    And a workspace whose template version reads "<workspace>"
    Then the app reports that workspace as "<reading>"

    Examples: Below the floor, within the minor release the app supports
      | app          | workspace         | reading         |
      | minds-v0.9.0 | minds-v0.9.0-rc.1 | security update |
      | minds-v0.9.2 | minds-v0.9.0-rc.3 | security update |

    Examples: Below the floor, on an earlier minor release than the app supports
      | app           | workspace    | reading         |
      | minds-v0.9.0  | minds-v0.8.7 | security update |
      | minds-v0.10.1 | minds-v0.8.3 | security update |

    Examples: At or above the floor
      | app           | workspace    | reading         |
      | minds-v0.9.2  | minds-v0.9.0 | patch available |
      | minds-v0.10.0 | minds-v0.9.0 | out of date     |
      | minds-v0.9.0  | minds-v0.9.0 | up to date      |

    Examples: The app's supported release is below the floor
      | app               | workspace    | reading         |
      | minds-v0.8.7      | minds-v0.8.3 | patch available |
      | minds-v0.8.7      | minds-v0.7.9 | out of date     |
      | minds-v0.9.0-rc.2 | minds-v0.8.7 | out of date     |

  @security-update-prompted
  Scenario: A workspace with a security update is prompted for it, more prominently than for an ordinary update
    Given the app supports a template release at or above the security floor
    And a workspace running a template release below the security floor, within the minor release the app supports
    Then the app badges that workspace as having a security update
    And the app's notice for that workspace says that the update includes a critical security fix
    And the app's explanation of that update offers details of what the security fix addresses
    And that badge and notice are drawn with more emphasis than an ordinary update's
    And an action covering every workspace with an update available includes that workspace
    And the app offers to update that workspace

  @security-floor-needs-a-released-app
  Scenario: A development build offers no security update
    Given the app is a development build with no supported template release
    And a workspace running a template release below the security floor
    Then the app reports that workspace's version as unknown
    And the app does not report that workspace as having a security update

  @patch-available-not-prompted
  Scenario: A workspace with only a patch available is not prompted to update
    Given the app supports a template release
    And a workspace running an earlier patch release of that same minor release
    Then the app shows no update badge or notice for that workspace
    And an action covering every workspace with an update available passes over that workspace

  @patch-available-by-hand
  Scenario: A workspace with only a patch available may still be updated by hand
    Given the app supports a template release
    And a workspace running an earlier patch release of that same minor release
    When the user asks to update that workspace
    Then an update agent is started in that workspace

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

  @too-old-is-missing-security-fixes
  Scenario: A workspace that needs recreating is told it is missing security fixes
    The in-place cutoff sits below the security floor, so every such workspace is below the floor as well.
    Given a workspace running a template release older than minds-v0.3.10
    Then the app reports that workspace as needing recreation
    And the app's notice for that workspace says that it is missing security fixes

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
  Rule: A workspace's version is read from what it is running; what it was created at counts only below the in-place cutoff
    The created-at version never changes, so it stops naming what a workspace runs at that workspace's first update; reporting it would offer such a workspace an update it already has.
    A workspace created at a release older than the oldest one that can be updated in place cannot have been updated past it, so for that workspace alone the created-at version still names what it runs.
    Any other workspace the app has not read is reported as unknown.
    A version read from a workspace holds while that workspace cannot be read and past a read that fails, because its version moves only when an update lands in it.
    A workspace that can be read again is read again before its earlier reading is relied on.
    A workspace created from a published template descends from a release without carrying that release's name, so the app resolves the name from the template rather than reporting unknown.
    It resolves a name that way only for a workspace the template could name; any other tree's version stays what its own history says, or below the in-place cutoff what it was created at.

    @updated-workspace-not-re-offered
    Example: A workspace that already updated itself is not offered the same update again
      Given a workspace created at an earlier template release
      And that workspace is running the supported template release
      Then the app reports that workspace as up to date

    @unread-workspace-is-unknown
    Example: A workspace the app has not read is unknown, not the version it was created at
      Given the app supports a template release
      And a workspace created at an earlier template release that can still be updated in place
      And the app has not read the template release that workspace is running
      Then the app reports that workspace's version as unknown
      And the app does not report that workspace as out of date

    @unread-below-cutoff-needs-recreation
    Example: A workspace created below the in-place cutoff needs recreating before the app has read it
      Given a workspace created at a template release older than minds-v0.3.10
      And the app has not read the template release that workspace is running
      Then the app reports that workspace as needing recreation
      And the app offers no update for that workspace

    @read-held-while-unreadable
    Example: A workspace that can no longer be read keeps the version it was read at
      Given a workspace created at an earlier template release
      And the app has read that workspace running the supported template release
      When that workspace can no longer be read
      Then the app still reports that workspace as up to date
      And the app does not report that version as what the workspace was created at

    @version-recovered-from-the-template
    Example: A workspace whose own history does not name its release is read from the template it came from
      Given the app supports a template release
      And a workspace created from a published template
      And that workspace's own history does not name the release it descends from
      Then the app reports that workspace's version as the release it descends from

    @version-not-recovered-for-an-unrelated-workspace
    Example: A workspace that did not come from the template is not read from it
      Given a workspace whose own history does not name a release
      And that workspace's tree did not come from the workspace template
      When the app reads that workspace's template version
      Then the app leaves that workspace's git untouched

    @read-held-past-a-failed-read
    Example: A read that fails does not displace the version already read
      Given a workspace created at an earlier template release
      And the app has read that workspace running the supported template release
      When the next read of that workspace fails
      Then the app still reports that workspace as up to date
      And the app does not report that version as what the workspace was created at
