Feature: Where a workspace's update is reported and acted on
  Over a workspace, its update is reported by a short status in the main window's titlebar, after the workspace's own controls, which takes no room from the workspace.
  The notice band under the titlebar is kept for what is wrong right now, and never carries a workspace's update.
  A workspace's Settings -> Updates tab is the one place its update is explained and acted on; every other surface that points at the update opens it, unless the answer lives in the update's chat.

  @status-in-the-titlebar
  Scenario Outline: The titlebar's status names what a workspace's update needs from the user
    Given the workspace on screen <situation>
    Then its titlebar status reads "<status>", drawn as <emphasis>
    And clicking it opens <destination>

    Examples:
      | situation                                                    | status                      | emphasis                  | destination               |
      | is out of date, and auto-update will not schedule it         | Update available            | information only          | its Updates tab           |
      | has a security update available                              | Security update available   | needing the user          | its Updates tab           |
      | has an update starting                                       | Updating…                   | information only, pulsing | its Updates tab           |
      | has an update preparing                                      | Updating…                   | information only, pulsing | the update's chat         |
      | has an update finishing its checks                           | Finishing update…           | information only, pulsing | the update's chat         |
      | has an update waiting for the user's answer or go-ahead      | Update needs a decision     | needing the user          | the update's chat         |
      | has an update that ended waiting for a decision              | Update needs a decision     | needing the user          | the update's chat         |
      | has an update that didn't start                              | Update didn't start         | needing the user          | its Updates tab           |
      | has an update that didn't finish                             | Update didn't finish        | needing the user          | the update's chat         |
      | needs a sign-in before its update can start                  | Update needs sign-in        | needing the user          | its Updates tab           |
      | is too old to update in place                                | Workspace too old to update | needing the user          | its Updates tab           |
      | has updated, and the update agent left a note for the user   | Updated, with a note        | information only          | the update's chat         |

  @no-status-when-nothing-is-asked
  Scenario: A workspace whose update asks nothing of the user has no titlebar status
    Given the workspace on screen is up to date, scheduled, skipped, of unknown version, ahead of the app, stopped, or cleanly updated
    Then its titlebar carries no update status

  @scheduled-said-only-in-the-tab
  Scenario: A scheduled update is said only in the workspace's Updates tab
    Given a workspace whose update is scheduled, by the user or by auto-update, including one with a security fix
    Then its titlebar carries no update status
    And its row on the home screen carries no update badge
    And its Updates tab says the update is scheduled, with a way to update now instead

  @status-opens-the-tab-without-a-chat
  Scenario: A status whose answer is in the update's chat opens the Updates tab while that chat is not known
    Given the workspace on screen has an update whose chat the app does not know
    Then clicking its titlebar status opens its Updates tab

  @outcome-read-from-the-status
  Scenario: Following a finished run's titlebar status dismisses how it ended
    The run's chat has the whole story, so reaching it is reading the outcome.
    Given the workspace on screen has an update that didn't finish, or that updated with a note from the update agent
    And the app knows the update's chat
    When the user clicks its titlebar status
    Then the update's chat opens
    And the outcome is dismissed, so the titlebar carries no update status
    But an update that needs a decision keeps its status, since dismissing a decision skips the release

  @outcome-read-in-the-tab
  Scenario: Leaving the Updates tab dismisses how a run ended
    Given a workspace whose last update didn't start, didn't finish, or updated, with or without a note
    When the user opens its Updates tab, however it was reached
    Then the tab shows how the run ended, with no Dismiss
    When the user leaves the tab, by closing the panel, opening the run's chat, or moving to another section
    Then the outcome is dismissed, so the titlebar carries no update status
    But leaving the tab dismisses nothing while it shows a decision, a run under way, an update on offer, or a stopped workspace
    And a Try again pressed on the way out keeps the outcome until the new run replaces it

  @no-status-while-applying
  Scenario: A workspace whose update is landing shows the apply, not a status
    Given the workspace on screen has an update that has reached its apply step
    Then the app covers that workspace with a notice that its update is landing, which nothing dismisses
    And its titlebar carries no update status

  @status-stands-down-for-the-band
  Scenario: The titlebar status stands down while the band speaks
    Given the workspace on screen has an update status
    When the band reports a connection or health problem with that workspace
    Then its titlebar carries no update status until the band clears

  @updates-tab-is-where-an-update-is-acted-on
  Scenario: Every surface pointing at an update opens the workspace's Updates tab
    Given a workspace whose update needs something from the user
    When the user opens it from the titlebar status, from the workspace's badge on the home screen, or from a press that failed elsewhere
    Then the workspace's Updates tab opens over the workspace, its status flashing once to show where to look
    And the tab says what the update needs, with the version jump, its actions, and the update agent's own words when it left any
    And a press that failed elsewhere is explained there, with the underlying error when there was one

  @updates-tab-nav-dot
  Scenario: The Updates entry of a workspace's settings carries the titlebar status's colour
    Given a workspace whose update earns a titlebar status
    Then the Updates entry in that workspace's settings is marked in the same colour
    And it carries no mark while there is no status

  @home-badge-opens-the-tab
  Scenario: A workspace's update badge on the home screen opens its Updates tab
    Given a workspace whose row on the home screen carries an update badge
    When the user clicks that badge
    Then the workspace's Updates tab opens
    But while that workspace's update is under way past starting, the badge opens the update's chat instead
