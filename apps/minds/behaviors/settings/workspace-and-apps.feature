Feature: The Workspace & apps tab
  The picker chooses what the tab speaks for, and the rail under it lists that pick's pages.
  Picking changes what Settings shows and never what the window shows.

  @picker-groups
  Scenario: The picker offers the account's workspaces, then what is shared with it
    Given a window for an account that owns the workspaces "garden", which is running, and "meadow", which is stopped
    And Sam Okafor shared the workspace "harbor" with the account, which the account accepted
    And Priya Nair shared the app "Recipes" of her workspace with the account, which the account accepted
    When the user opens the picker, labelled "Workspace/App", on the Workspace & apps tab
    Then it offers "garden" and "meadow" under "Your workspaces"
    And "harbor" under "Workspaces shared with you"
    And "Recipes (Priya Nair)" under "Apps shared with you"

  @picker-plain-when-nothing-shared
  Scenario: With nothing shared, the picker is a plain list of the account's workspaces
    Given a window for an account that owns workspaces, with nothing shared with it
    When the user opens the picker
    Then it offers the account's workspaces with no group headings

  @picker-leaves-out
  Scenario Outline: The picker leaves out what has no settings to show here
    Given <row> in the window's account
    When the user opens the picker
    Then it does not offer that <kind>

    Examples:
      | row                                                                              | kind      |
      | a workspace still being created                                                  | workspace |
      | a workspace this device knows only from the account's synced records             | workspace |
      | a share the account has not answered                                             | share     |
      | a share the account declined                                                     | share     |

  @pick-does-not-move-the-window
  Scenario: Picking changes what Settings shows, not what the window shows
    Given a window showing the account's workspace "garden", with Settings open on it
    When the user picks "meadow"
    Then Settings shows "meadow"'s pages
    And the window still shows "garden" behind Settings, and the trail still names "garden"

  @pick-keeps-the-entry
  Scenario: A new pick opens on the entry the last one was on, at its own start
    Given Settings is open on the Sharing page of an app of "garden"
    When the user picks "meadow"
    Then Settings shows "meadow"'s Sharing, on the whole workspace's page

  @pick-defaults
  Scenario Outline: With no pick named, the tab speaks for the workspace on screen, else the first on offer
    Given the Workspace & apps tab is opened by an address that names no pick, in a window showing <content>
    Then the tab speaks for <pick>

    Examples:
      | content                              | pick                           |
      | one of the account's own workspaces  | that workspace                 |
      | the account's home page              | the first row the picker offers |

  @pick-that-disappears
  Scenario Outline: A pick that disappears falls back
    Given Settings shows a pick that then disappears, because <cause>
    Then the tab speaks for the workspace on screen, else for the first row the picker offers
    And with nothing left to offer, Settings shows the Studio tab

    Examples:
      | cause                                           |
      | the workspace was destroyed                     |
      | the workspace moved to another account          |
      | its owner stopped sharing it with the account   |

  @own-workspace-rail
  Scenario: One of the account's own workspaces has six entries
    Given the pick is one of the account's own workspaces
    Then the rail under the picker lists General, Permissions, Sharing, Backup, Updates, and Suggestions
    And it opens on General unless the address names another

  @nested-entries
  Scenario Outline: Permissions and Sharing list their own pages under their entry while it is selected
    Given the pick is one of the account's own workspaces
    When the user chooses <entry> in the rail
    Then under that entry the rail lists <nested pages>
    And choosing another entry of the rail folds them away

    Examples:
      | entry       | nested pages                                                                              |
      | Permissions | the workspace's permission sections                                                       |
      | Sharing     | "Full workspace", then one page per app, then the system apps under a "System apps" label |

  @permissions-entry-marks-waiting-requests
  Scenario: The Permissions entry is marked while the pick's agents wait on an answer
    Given the pick is a workspace of the account's own with a permission request the user has not answered
    Then the Permissions entry of the rail carries a dot

  @review-returns-to-settings
  Scenario: Reviewing a permission request from Settings comes back to Settings
    Given Settings is open on the Permissions of a workspace with a request waiting
    When the user opens the request to review it
    Then the request opens as a dialog in the middle of the window
    When the user answers or dismisses it
    Then Settings is open on that workspace's Permissions again

  @owner-is-in-general
  Scenario: The workspace's account is the Owner section of General
    Given the pick is one of the account's own workspaces
    When the user opens its General page
    Then an "Owner" section names the account the workspace belongs to, and is where that account is changed
    And every "link an account" prompt elsewhere in the workspace's settings opens General at that section

  @unreadable-settings
  Scenario: A workspace whose settings cannot be read says so and can be tried again
    Given the pick is a workspace of the account's own whose settings cannot be read
    Then its General, Sharing, Backup, Updates, and Suggestions pages show a notice that the settings could not be loaded, with "Try again"
    And its Permissions page reports its own state independently

  @shared-pick-rail
  Scenario Outline: A workspace or app shared with the account has one entry, About
    Given the pick is <pick>
    Then the rail under the picker lists About alone
    And none of an owner's pages is offered

    Examples:
      | pick                                |
      | a workspace shared with the account |
      | an app shared with the account      |
