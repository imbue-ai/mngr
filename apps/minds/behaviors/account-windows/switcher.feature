Feature: The account switcher
  The titlebar control that names the window's account and moves the window between accounts.

  @names-the-account
  Scenario Outline: The switcher names the window's account
    Given a window for <account>
    Then the switcher shows <label>

    Examples:
      | account                                                                  | label                   |
      | the device                                                               | "This device"           |
      | the signed-in account "alice@example.com" with no display name           | "alice@example.com"     |
      | the signed-in account "alice@example.com" with the display name "Alice"  | "Alice"                 |

  @lists-every-account
  Scenario: The switcher lists every account the window could move to
    Given the accounts "alice@example.com" and "bob@example.com" are signed in
    And the account "carol@example.com" is signed out but has workspaces on this installation
    And the account "dave@example.com" is signed out and has no workspace on this installation
    When the user opens the switcher in a window for Alice
    Then it lists Alice, marked as the window's own account and not actionable
    And it lists Bob, Carol marked as signed out, and the device
    And it does not list Dave
    And it offers "Add account", which starts the browser sign-in

  @switch-lands-on-a-free-recent-workspace
  Scenario: Switching lands on the account's most recently opened workspace that no other window shows
    Given the account "bob@example.com" has the workspaces "older" and "newer" on this installation
    And a window opened "older" and then a window opened "newer"
    And no other window currently shows "newer"
    When the user chooses Bob in the switcher of a window for Alice
    Then that window shows the workspace "newer"

  @switch-skips-a-workspace-another-window-shows
  Scenario: A workspace another window shows is skipped
    Given the account "bob@example.com" has the workspaces "older" and "newer" on this installation
    And a window opened "older" and then a window opened "newer"
    And another window currently shows "newer"
    When the user chooses Bob in the switcher of a window for Alice
    Then that window shows the workspace "older"

  @switch-falls-back-to-the-home-page
  Scenario Outline: With no free recently opened workspace, switching lands on the account's home page
    Given <situation>
    When the user chooses Bob in the switcher of a window for Alice
    Then that window shows Bob's home page

    Examples:
      | situation                                                                                      |
      | the account "bob@example.com" has no workspace on this installation                            |
      | every workspace of "bob@example.com" that a window has opened is currently shown in another window |

  @switch-to-signed-out-account
  Scenario: Switching to a signed-out account shows its state, signed out
    Given the account "carol@example.com" is signed out but has workspaces on this installation
    When the user chooses Carol in the switcher
    Then that window is for Carol, lists her workspaces, and its home page offers to log in

  @sign-in-adopts-the-account
  Scenario: A sign-in asked for by a page adopts the account into the window without leaving the page
    Given a window for the device on the new-workspace form
    When the user asks to create on Imbue Cloud and signs in as "alice@example.com" in the browser
    Then the window becomes a window for Alice, still on the form
    And the workspace it creates belongs to Alice

  @new-window-follows-the-focused-account
  Scenario: A new window opens on the focused window's account
    Given the focused window is for the account "alice@example.com"
    When the user opens a new window
    Then the new window shows Alice's home page

  @restore-keeps-the-account
  Scenario: A window on a home page is restored on the same account
    Given a window for the account "alice@example.com" was on her home page when the app last quit
    When the app launches again and restores its windows
    Then that window comes back on Alice's home page

  @workspace-move-leaves-the-window
  Scenario: Moving a workspace to another account sends the window to its own account's home page
    Given a window for the account "alice@example.com" shows one of her workspaces
    When the user associates that workspace with the account "bob@example.com" from the workspace's options
    Then the window shows Alice's home page
