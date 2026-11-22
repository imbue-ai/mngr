Feature: The workspace switcher
  The trail's control that names the workspace on screen and opens a menu of what the window could show.
  The menu lists every choice, what is on screen among them: that row is marked, and choosing it only closes the menu.

  @menu-sections
  Scenario: The menu lists the account's workspaces, then what is shared with it
    Given a window for an account showing its own workspace "garden"
    And the account also owns the workspaces "orchard" and "meadow"
    And Sam Okafor shared the workspace "harbor" with the account, which the account accepted
    And Priya Nair shared the app "Recipes" of her workspace with the account, which the account accepted
    When the user opens the workspace switcher
    Then under a search field the menu lists "garden", "orchard" and "meadow" under "Your workspaces", each beside its colour
    And that section ends with "New workspace", which opens the new-workspace form, with no rule above it
    And then "harbor" under "Workspaces shared with you", naming Sam Okafor at its right
    And then "Recipes" under "Apps shared with you", naming Priya Nair at its right
    And last, under a rule, "Workspace & app settings", which opens Settings on "garden"
    And every row starts at the same left edge, its colour, tile or icon in one column

  @first-heading
  Scenario Outline: The first heading reads "Your workspaces" whatever the window shows
    Given an account that owns workspaces
    And a window for it showing <content>
    When the user opens the workspace switcher's menu
    Then the account's own workspaces are listed under "Your workspaces"

    Examples:
      | content                                             |
      | one of the account's own workspaces                 |
      | the account's home page                             |
      | a workspace another person shared with the account  |
      | the new-workspace form, with the switcher shortcut  |

  @own-section-always-shown
  Scenario: "Your workspaces" is shown with no workspace to list under it
    Given a window for an account that owns no workspace
    When the user opens the workspace switcher
    Then the menu shows "New workspace" under "Your workspaces"

  @empty-sections-left-out
  Scenario: A shared section with nothing to list is left out with its heading
    Given a window for an account showing the only workspace it owns
    And nothing is shared with the account
    When the user opens the workspace switcher
    Then the menu shows the search field, that workspace and "New workspace" under "Your workspaces", and "Workspace & app settings"
    And it shows no other section heading

  @on-screen-row-is-marked
  Scenario Outline: What is on screen is listed in its section, marked
    Given a window showing <content>
    When the user opens the workspace switcher
    Then the menu lists <row>
    And that row carries a check at its right and a tint fainter than the highlight's
    And no other row is marked

    Examples:
      | content                                          | row                                                |
      | one of the account's own workspaces              | that workspace under "Your workspaces"             |
      | a workspace shared with the account              | that workspace under "Workspaces shared with you"  |
      | an app shared with the account, open on its own  | that app under "Apps shared with you"              |

  @choosing-the-row-on-screen
  Scenario: Choosing the row of what is on screen only closes the menu
    Given the workspace switcher's menu is open in a window showing a workspace
    When the user chooses that workspace's row
    Then the menu closes
    And the window keeps showing the workspace, which is not loaded again

  @menu-leaves-out
  Scenario Outline: The menu leaves out what the account has not accepted
    Given <situation>
    When the user opens the workspace switcher
    Then the menu does not list <left out>

    Examples:
      | situation                                                                               | left out    |
      | another person shared a workspace with the account, which the account has not answered  | that share  |
      | another person shared a workspace with the account, which the account declined          | that share  |

  @last-row-is-about-the-workspace-on-screen
  Scenario Outline: The last row is about the workspace on screen
    Given a main window showing <content>
    When the user opens the workspace switcher
    Then the menu ends with <last row>

    Examples:
      | content                                             | last row                                                                      |
      | one of the account's own workspaces                 | "Workspace & app settings", which opens Settings on that workspace            |
      | a workspace Sam Okafor shared with the account      | "Shared by Sam Okafor", which opens Settings on that workspace's About page   |
      | a workspace opened from a link the account pasted   | "About this link", which opens Settings on that link's About page             |
      | the account's home page                             | the last row of its sections, with no row about a workspace after it          |

  @choosing-a-row
  Scenario Outline: Choosing a row closes the menu and opens the row in this window
    Given the workspace switcher's menu lists <row>
    When the user chooses it
    Then the menu closes and the window shows <result>

    Examples:
      | row                                               | result                                 |
      | one of the account's own workspaces               | that workspace                         |
      | a workspace of the account's still being created  | that workspace's creation page         |
      | a workspace shared with the account               | that workspace, opened as a guest      |
      | an app shared with the account                    | that app on its own, opened as a guest |

  @creation-state-on-the-row
  Scenario Outline: A workspace still being created is listed with how its creation stands
    Given the account owns a workspace whose creation <state>
    When the user opens the workspace switcher
    Then that workspace's row reads "<badge>" at its right

    Examples:
      | state            | badge         |
      | is running       | Creating…     |
      | was interrupted  | Interrupted   |
      | failed           | Create failed |

  @run-state-on-the-row
  Scenario: A workspace that is not running is marked with its state
    Given the account owns a workspace that is stopped, stopping, or starting, or whose state is unknown
    When the user opens the workspace switcher
    Then that workspace's row carries a mark naming that state
    And the row of a running workspace carries none

  @unreachable-row-is-inert
  Scenario: A workspace this device cannot reach is listed but cannot be chosen
    Given the account owns a workspace this device knows only from the account's synced records, hosted on another device or out of this device's sight
    When the user opens the workspace switcher
    Then that workspace's row is dimmed
    And choosing it does nothing

  @open-in-new-window
  Scenario: A row of the account's own workspaces offers to open it in a new window
    Given the desktop app, with the workspace switcher's menu listing one of the account's own workspaces that exists on this device
    When the user chooses "Open in new window" on that row
    Then the menu closes and that workspace is shown in another window
    And this window keeps what it shows
    And no such control is on the row of the workspace on screen, of a workspace still being created, of a workspace this device cannot reach, or of anything shared with the account
    And no row offers it in a plain browser

  @refused-open-is-reported
  Scenario: A shared workspace that cannot be opened is reported
    Given the workspace switcher's menu lists "harbor", a workspace shared with the account
    When the user chooses it and the open is refused
    Then a dialog titled "Couldn't open harbor" says why
    And the menu stays closed

  @search-field
  Scenario: The menu opens with its search field focused, naming the switcher shortcut
    When the user opens the workspace switcher
    Then the search field, whose placeholder reads "Search workspaces and apps", has keyboard focus
    And at its right the field names the switcher shortcut's current chord
    And it names none while the shortcut is turned off

  @search-narrows
  Scenario Outline: Typing narrows every section to the rows that match
    Given the menu lists the account's own "orchard", which is on screen, and "meadow", and "harbor" shared by Sam Okafor
    When the user types "<typed>" in the search field
    Then the menu lists <rows left>
    And a shared section left with no row loses its heading
    And "New workspace" stays under "Your workspaces"

    Examples:
      | typed  | rows left                                         |
      | MEA    | "meadow" alone                                    |
      | okafor | "harbor" alone, matched by its owner's name       |
      | arb    | "harbor" alone, matched anywhere in its name      |

  @search-no-match
  Scenario: A search that matches nothing says so
    Given the workspace switcher's menu is open
    When the user types text that no listed name and no owner contains
    Then the menu reads "No workspaces or apps match." in place of its lists
    And "New workspace" is still offered under "Your workspaces"

  @closing-clears-the-search
  Scenario: Closing the menu forgets the search
    Given the user typed in the workspace switcher's search field and closed the menu
    When the user opens the workspace switcher again
    Then the search field is empty, every row is listed, and no row is highlighted

  @one-highlight
  Scenario: The arrow keys and the pointer move one highlight
    Given the workspace switcher's menu is open, listing several rows
    When the user presses the down arrow
    Then the first row is highlighted, and typing still goes to the search field
    When the user presses the down arrow again
    Then the highlight moves to the next row, through the rows in the order they are drawn, the row of what is on screen, "New workspace" and the last row included
    When the user moves the pointer over another row
    Then that row alone is highlighted
    When the user presses the up arrow from the first row
    Then no row is highlighted

  @enter-opens
  Scenario Outline: Enter opens the highlighted row, else the first row listed
    Given the workspace switcher's menu is open and <highlight>
    When the user presses Enter
    Then the menu opens <opened>

    Examples:
      | highlight               | opened                                                                                                                                                   |
      | a row is highlighted    | that row                                                                                                                                                 |
      | no row is highlighted   | the first row that can be chosen and is not on screen: of the account's own workspaces, else of the workspaces shared with it, else of the apps shared   |

  @enter-with-only-the-row-on-screen
  Scenario: Enter closes the menu when the search leaves only the row of what is on screen
    Given the workspace switcher's menu is open in a window showing a workspace, with no row highlighted
    When the user types that workspace's name, which no other row matches, and presses Enter
    Then the menu closes and the window keeps showing the workspace

  @account-menu-keys
  Scenario: The account switcher's menu is driven by the same keys
    When the user opens the account switcher
    Then the menu has keyboard focus
    And the arrow keys move one highlight, which the pointer shares, through its rows
    And Enter chooses the highlighted row
