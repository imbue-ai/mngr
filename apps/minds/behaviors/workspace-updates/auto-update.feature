Feature: Automatic workspace updates
  Auto-update is one app-wide setting: when it is on, the app schedules every workspace that is a minor release behind, so the user never has to.
  Updating spends tokens against the user's subscription or API usage, so it stays off until the user chooses it.

  @off-until-chosen
  Scenario: Auto-update is off until the user turns it on, and is offered once
    Given the user has never chosen whether to update workspaces automatically
    When the app relaunches into a new version of itself, or first finds a workspace out of date
    Then the app offers to turn on automatic updates, saying what they cost
    And answering the offer either way means it is never shown again
    And until the user turns it on, no workspace is scheduled for them

  @arms-behind-workspaces
  Scenario: With auto-update on, every workspace a minor release behind is scheduled
    A workspace with only a patch available is not scheduled on its own; the patch rides along with the next minor release.
    Given auto-update is on
    And a running workspace is read as a minor release behind
    Then that workspace's update is scheduled for its next window
    And a workspace with only a patch available is not scheduled

  @quiet-under-auto-update
  Scenario: With auto-update on, an out-of-date workspace is not nagged about
    Given auto-update is on
    And a workspace that auto-update will schedule
    Then the workspace view carries no out-of-date notice for it
    And the app offers no Schedule or Update all action
    And the workspace's row says its update is scheduled, and its update says no action is needed
    And the app still reports a landed update, and a workspace too old to update in place

  @skip-this-update
  Scenario: Skipping an update holds one workspace back until the next release
    Given auto-update is on
    And a workspace with a scheduled update
    When the user skips that update
    Then that workspace is no longer scheduled
    And it is not scheduled again until the app moves to a newer release
    And its row shows it as out of date again, with its own update actions

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
    Then that workspace is not scheduled again until the app moves to a newer release, or the user dismisses the failure
