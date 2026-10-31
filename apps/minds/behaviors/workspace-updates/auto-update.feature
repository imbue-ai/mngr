Feature: Automatic workspace updates
  Auto-update schedules a workspace that is a minor release behind or below the security floor, so the user never has to.
  Studio's own setting is the default every workspace follows: a switch, and the window scheduled updates start in.
  A workspace may instead have its own setting: auto-update on in its own window whatever Studio's switch says, or never auto-updated at all.
  A workspace's *effective* setting is its own when it has one, else Studio's; every statement below about auto-update being on or off for a workspace means its effective setting.
  Each workspace's own setting is kept on this computer, alongside the releases skipped for it.
  Updating spends tokens against the user's subscription or API usage, so it stays off until the user chooses it.

  @off-until-chosen
  Scenario: Auto-update is off until the user turns it on, and is offered once
    Given the user has never chosen whether to update workspaces automatically
    When the app relaunches into a new version of itself, or first finds a workspace that follows Studio settings out of date
    Then the app offers to turn on automatic updates, saying what they cost
    And answering the offer either way means it is never shown again
    And until the user turns it on, no workspace is scheduled for them

  @offer-pitched-in-the-updates-tab
  Scenario: A workspace's Updates tab makes the offer while it is unanswered
    A user who never saw the offer meets it where they look after their updates.
    Given the user has never answered the offer to turn on automatic updates
    And Studio's auto-update switch is off
    When the user opens the Updates tab of a workspace that follows Studio settings
    Then its automatic-updates section makes the offer, with a window to pick, in place of the workspace's own choices
    But the Updates tab of a workspace with its own setting shows its own choices, with that setting chosen
    When the user turns automatic updates on there, or declines
    Then the offer is answered, and is never shown again anywhere
    And the section shows the workspace's own choices

  @arms-behind-workspaces
  Scenario: With auto-update on, every workspace a minor release behind is scheduled
    A workspace with only a patch available is not scheduled on its own; the patch rides along with the next minor release.
    Given auto-update is on
    And a running workspace is read as a minor release behind
    Then that workspace's update is scheduled for its next window
    And a workspace below the security floor is scheduled the same way, for its next window, however small its gap
    And a workspace with only a patch available is not scheduled

  @per-workspace-auto-update-setting
  Scenario Outline: A workspace's own setting decides whether it is auto-updated, whatever Studio's switch says
    Given Studio's auto-update switch is <studio>
    And a running workspace that is a minor release behind, with its own setting <own>
    Then that workspace <outcome>
    And every other workspace follows Studio's switch

    Examples:
      | studio | own                                 | outcome                                         |
      | off    | of a custom window                  | is scheduled for the next opening of its window |
      | on     | of updating manually                | is not scheduled                                |
      | on     | removed, so it follows Studio again | is scheduled for Studio's next window           |

  @custom-window-is-the-workspaces-own
  Scenario: A workspace with a custom window has its updates start in that window
    Given a workspace with its own window
    Then every read of that workspace's updates tells it its own window, not Studio's
    And a scheduled update of that workspace starts only inside its own window
    And changing Studio's window leaves that workspace's window as it is

  @manual-keeps-schedules
  Scenario: Setting a workspace to update manually leaves a schedule auto-update made
    Given a workspace whose update auto-update scheduled
    When the user sets that workspace to update manually
    Then that workspace's update is still scheduled
    And its Updates tab shows it as an ordinary schedule, with a way to cancel it

  @custom-settings-listed-in-studio
  Scenario: Studio settings lists the workspaces that use their own setting
    Given a workspace with its own auto-update setting
    When the user opens Studio settings' workspace updates
    Then that workspace is listed with its own setting and, when auto-updated, its own window
    And its name opens that workspace's Updates tab
    When the user asks that workspace to use Studio settings there
    Then it follows Studio's setting at once, without being asked to confirm
    And workspaces that no longer exist are not listed

  @removed-workspace-forgets-its-setting
  Scenario: Removing a workspace forgets its own setting
    Given a workspace with its own auto-update setting
    When Studio finishes removing that workspace
    Then its setting is gone from this computer
    And every other workspace keeps its own

  @quiet-under-auto-update
  Scenario: A workspace auto-update will schedule is not nagged about
    A workspace below the security floor is no exception: auto-update schedules it like any other, so it is not nagged about either.
    Given a workspace that auto-update will schedule
    Then the workspace's titlebar carries no update status for it
    And the workspace's row says nothing about its update, before or after auto-update schedules it
    And once it is scheduled, its update says no action is needed
    And the app still reports a landed update, and a workspace too old to update in place

  @update-all-by-effective-setting
  Scenario: The Update all action covers only the workspaces auto-update will not take care of
    Given several workspaces with an update available
    Then the app offers to schedule or update all of them at once only when two or more of them are not ones auto-update will schedule
    And that action covers only those workspaces

  @skip-this-update
  Scenario: Skipping an update holds one workspace back until the next release
    Given auto-update is on
    And a workspace whose update auto-update scheduled
    When the user asks to skip that update
    Then the app says which release is skipped and what skipping it means, and skips it only once the user confirms
    When the user confirms
    Then that workspace is no longer scheduled
    And it is not scheduled again until the app moves to a newer release
    And the workspace's titlebar carries no update status for it
    And its row says the release was skipped, with a way to update it by hand

  @schedule-by-hand-under-auto-update
  Scenario: With auto-update on, the user can still schedule a workspace's update by hand
    Given auto-update is on
    Then a workspace's update can be scheduled by hand, unless it is already scheduled to the version the app supports
    And a specific version can be scheduled for any workspace
    And scheduling the supported version over a schedule for another version replaces that schedule
    When the user schedules an update
    Then its update says it is scheduled, with a way to cancel the schedule rather than skip the release
    And auto-update leaves that schedule as the user made it

  @sign-in-blocks-auto-update
  Scenario: A workspace that cannot be scheduled for want of a sign-in says so
    Given auto-update is on
    And an out-of-date workspace with no AI account signed in
    Then the workspace's row shows it as out of date, needing a sign-in to update automatically
    And the app tries to schedule it again on every read until an account is signed in

  @turning-off-keeps-schedules
  Scenario: Turning auto-update off leaves the schedules already made
    Given auto-update is on
    And a workspace with a scheduled update
    When the user turns auto-update off
    Then that workspace's update is still scheduled

  @failed-run-not-rescheduled
  Scenario: A run that failed is not scheduled again for the same release
    Given auto-update is on
    And a workspace whose update failed
    Then that workspace is not scheduled again until the app moves to a newer release
    And dismissing the failure, which reading it does, does not schedule it again
    And once dismissed, the workspace says it is behind as it would without auto-update
    But the user can still update it to that release by hand
