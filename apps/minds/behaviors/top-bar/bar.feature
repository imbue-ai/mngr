Feature: What the top bar shows
  The trail names where the window is: the Studio wordmark, then the account switcher, then the workspace switcher or the name of the page.
  The right side offers the notification bell, Report a bug, and Settings, and Share where the workspace on screen is the account's to share.

  @trail-follows-the-window
  Scenario Outline: The trail's last place follows what the window shows
    Given a main window showing <content>
    Then after the account switcher the trail shows <last place>

    Examples:
      | content                                                                                   | last place                                                                 |
      | the account's home page                                                                   | a workspace switcher reading "Open workspace"                              |
      | "garden", one of the account's own workspaces                                             | a workspace switcher reading "garden" beside the workspace's colour        |
      | the page of "garden" while it is still being created                                      | a workspace switcher reading "garden" beside the workspace's colour        |
      | "orchard", a workspace Sam Okafor shared with the account                                 | a workspace switcher reading "orchard (Sam Okafor)"                        |
      | "Recipes", an app of Sam Okafor's workspace shared with the account, open on its own      | a workspace switcher reading "Recipes (Sam Okafor)" beside the app's tile  |
      | a workspace opened from a link the account pasted                                         | a workspace switcher reading the workspace's name, with no owner after it  |
      | the new-workspace form                                                                    | the page's name, "New workspace", and no workspace switcher                |
      | the recently destroyed workspaces page                                                    | the page's name, "Recently destroyed", and no workspace switcher           |

  @wordmark-goes-home
  Scenario: The wordmark goes to the home page of the window's account
    Given a window for the account "alice@example.com" showing one of her workspaces
    When the user chooses the Studio wordmark at the start of the trail
    Then the window shows Alice's home page

  @signed-out-mark
  Scenario: The account switcher marks an account that has no session here
    Given a window for the account "carol@example.com", which is signed out but has workspaces on this installation
    Then the account switcher reads "carol@example.com" followed by "(signed out)"

  @right-side-controls
  Scenario: The right side offers notifications, Report a bug, and Settings
    Given a main window outside the first-run start flow
    Then the right side of the bar offers the notification bell, Report a bug, and Settings, in that order

  @share-only-for-own-workspaces
  Scenario Outline: Share is offered only where the workspace on screen is the account's to share
    Given a main window showing <content>
    Then the bar <offering> Share, after Settings

    Examples:
      | content                                             | offering       |
      | one of the account's own workspaces                 | offers         |
      | the page of a workspace still being created         | offers         |
      | a workspace another person shared with the account  | does not offer |
      | the account's home page                             | does not offer |

  @share-before-the-workspace-exists
  Scenario: Share on a workspace still being created says to come back
    Given a main window showing the page of a workspace still being created
    When the user chooses Share
    Then a dialog reads "Check back here after the workspace is created to share it."
    And Settings does not open

  @settings-marks-waiting-requests
  Scenario: Settings is marked while the workspace on screen waits on a permission request
    Given the workspace on screen has a permission request the user has not answered
    Then the bar's Settings button carries a dot
    When the user opens or clears the notification about that request without answering it
    Then the dot stays
    When the user answers the request
    Then the dot goes

  @narrow-window-shortens-the-trail
  Scenario: A window too narrow for the trail shortens the names, never the controls
    Given a window too narrow to show the whole trail
    Then the account's email and the workspace's name are cut short
    And every control at the right of the bar keeps its size
