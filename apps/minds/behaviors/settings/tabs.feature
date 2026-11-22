Feature: Settings and its three tabs
  Settings opens on the place that fits what the window shows, and its three tabs hold the app's settings, the accounts, and the pick's pages.

  @settings-opens-on
  Scenario Outline: The bar's Settings button opens on what the window shows
    Given a main window showing <content>
    When the user chooses Settings in the bar
    Then Settings opens on <place>

    Examples:
      | content                                                                         | place                                                           |
      | the account's home page                                                         | the Studio tab                                                  |
      | one of the account's own workspaces                                             | the Workspace & apps tab, on that workspace's General page      |
      | one of the account's own workspaces, whose agents wait on a permission request  | the Workspace & apps tab, on that workspace's Permissions       |
      | a workspace another person shared with the account                              | the Workspace & apps tab, on that workspace's About page        |

  @three-tabs
  Scenario: Settings has three tabs
    Given a window for an account that owns a workspace
    When the user opens Settings
    Then under the title "Settings" a row of tabs offers "Studio", "Accounts", and "Workspace & apps"
    And the Studio tab holds the app's own sections
    And the Accounts tab holds one entry per signed-in account
    And the Workspace & apps tab holds the pick's pages under the picker

  @workspace-tab-needs-a-pick
  Scenario: The Workspace & apps tab is offered only while there is something to pick
    Given a window for an account with nothing the picker could offer
    When the user opens Settings
    Then the row of tabs offers "Studio" and "Accounts" only
    And an address that names the Workspace & apps tab shows the Studio tab

  @tabs-keep-their-place
  Scenario: A tab comes back to the entry it was left on
    Given Settings is open on the Studio tab's "Updates" section
    When the user chooses the Accounts tab and then the Studio tab again
    Then the Studio tab shows "Updates"

  @address-carries-the-place
  Scenario: A reload comes back to the same place in Settings
    Given Settings is open on the Sharing page of an app of one of the account's workspaces
    When the window is reloaded
    Then Settings is open on that app's Sharing page of that workspace

  @moving-adds-no-history
  Scenario: Moving around inside Settings adds nothing to the window's history
    Given the user opened Settings from a workspace, and then changed its tab, its entry, and its pick
    When the user steps back once in the window's history
    Then the window shows the workspace, with Settings closed

  @accounts-tab
  Scenario: The Accounts tab lists every signed-in account
    Given the accounts "alice@example.com" and "bob@example.com" are signed in
    When the user opens Settings on the Accounts tab in a window for Alice
    Then the rail lists "alice@example.com", marked "Current", and "bob@example.com"
    And the rail ends with "Add account", which starts the browser sign-in
    And Alice's entry is selected, showing her account

  @accounts-tab-in-the-device-window
  Scenario: In the device's window the Accounts tab opens on the first signed-in account
    Given the accounts "alice@example.com" and "bob@example.com" are signed in
    When the user opens Settings on the Accounts tab in a window for the device
    Then no entry is marked "Current"
    And the first account's entry is selected

  @accounts-tab-empty
  Scenario: With no account signed in, the Accounts tab says so
    Given no account is signed in
    When the user opens Settings on the Accounts tab
    Then the page reads "No accounts logged in."
    And the rail holds only "Add account"

  @logged-out-entry-falls-back
  Scenario: Logging out the selected account selects another
    Given Settings is open on the Accounts tab in a window for Alice, with Bob's entry selected
    When the user logs Bob out
    Then Bob's entry leaves the rail and Alice's entry is selected

  @home-card-opens-workspace-settings
  Scenario: A home page card opens its workspace's settings without opening the workspace
    Given the account's home page lists the workspace "garden"
    When the user chooses "Workspace settings" in the menu of "garden"'s card
    Then Settings opens on the Workspace & apps tab, on "garden"'s General page
    And the window still shows the home page behind Settings

  @studio-settings-crumb
  Scenario: The home page's "Studio settings" opens the Studio tab
    Given the account's home page
    When the user chooses "Studio settings"
    Then Settings opens on the Studio tab
