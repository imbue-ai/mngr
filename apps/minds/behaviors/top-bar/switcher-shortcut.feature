Feature: The switcher shortcut
  One key chord opens the workspace switcher's menu from anywhere in a main window, and closes it again.
  The desktop app holds the chord for the whole installation, and lets the user change it or turn it off.

  @default-chord
  Scenario Outline: The default chord depends on the platform
    Given the desktop app on <platform>, whose switcher shortcut has never been changed
    Then the switcher shortcut is <chord>

    Examples:
      | platform            | chord        |
      | macOS               | Cmd+K        |
      | any other platform  | Ctrl+Shift+K |

  @shortcut-toggles-the-menu
  Scenario: The shortcut opens the menu, and closes it when it is open
    Given the desktop app, with a main window showing a workspace and keyboard focus inside the workspace
    When the user presses the switcher shortcut
    Then the workspace switcher's menu opens with its search field focused
    And the key press does not reach the workspace
    When the user presses the switcher shortcut again
    Then the menu closes

  @shortcut-replaces-an-open-flyout
  Scenario: The shortcut puts away whichever flyout is open
    Given a main window with Settings open
    When the user presses the switcher shortcut
    Then Settings closes and the workspace switcher's menu opens

  @shortcut-without-a-switcher
  Scenario: Where the trail shows no workspace switcher, the menu hangs from the page's name
    Given a main window showing the new-workspace form
    When the user presses the switcher shortcut
    Then the workspace switcher's menu opens under the page's name in the trail

  @shortcut-does-nothing
  Scenario Outline: The shortcut opens nothing where there is no switcher, or a question is waiting
    Given <situation>
    When the user presses the switcher shortcut
    Then no menu opens

    Examples:
      | situation                                                                  |
      | the window shows the first-run start flow                                  |
      | a dialog that is not a flyout is waiting on the user                       |
      | the window is a pulled-out window                                          |
      | a workspace has taken the window's whole keyboard in immersive mode        |

  @held-chord-toggles-once
  Scenario: Holding the chord down toggles the menu once
    Given the desktop app, with a main window whose workspace switcher's menu is closed
    When the user presses the switcher shortcut and holds it
    Then the menu opens and stays open

  @record-a-chord
  Scenario: The user records a new chord in Settings
    Given the desktop app, with Settings on the Studio tab's "Keyboard shortcuts"
    When the user chooses "Change" on "Open the workspace switcher"
    Then the row reads "Press a shortcut…"
    When the user presses a chord of Cmd, Ctrl, Alt, or Super with one other key, which nothing else uses
    Then the row names that chord
    And that chord is the switcher shortcut at once, in every window of the app
    And the workspace switcher's search field names it

  @recording-takes-every-key
  Scenario: While a chord is being recorded, a key press does nothing else
    Given the user chose "Change" on "Open the workspace switcher"
    When the user presses the current switcher shortcut
    Then the workspace switcher's menu does not open
    And the press is taken as the chord being recorded

  @recording-ends-unchanged
  Scenario Outline: A recording that ends without a chord leaves the shortcut as it was
    Given the user chose "Change" on "Open the workspace switcher"
    When <ending>
    Then the recording ends and the switcher shortcut is unchanged

    Examples:
      | ending                              |
      | the user presses Escape             |
      | the user chooses "Cancel"           |
      | the window loses focus              |
      | Settings closes                     |

  @refused-chords
  Scenario Outline: A chord that cannot be the shortcut is refused, with the reason
    Given the user chose "Change" on "Open the workspace switcher"
    When the user presses <chord>
    Then the switcher shortcut is unchanged
    And under the row a line says <reason>

    Examples:
      | chord                                                                | reason                                                               |
      | a key with none of Cmd, Ctrl, Alt, or Super held                     | that a shortcut needs one of them held down with one other key       |
      | the chord that closes the window                                     | that the chord is already used, naming the command that uses it      |
      | the chord the platform uses to paste                                 | that the chord is already used, naming the command that uses it      |

  @reset-and-turn-off
  Scenario: The shortcut can be put back to the default, or turned off
    Given the desktop app, whose switcher shortcut was changed from the default
    Then the row offers "Reset to default" and "Turn off", and names the default under it
    When the user chooses "Reset to default"
    Then the default chord is the switcher shortcut again
    When the user chooses "Turn off"
    Then the row reads "Off", and no chord opens the workspace switcher's menu
    And the workspace switcher's search field names no chord
    And the row offers "Turn on", which makes the default chord the shortcut

  @shortcut-is-kept
  Scenario: The chosen shortcut is kept across launches
    Given the user changed the switcher shortcut, or turned it off
    When the app quits and launches again
    Then the switcher shortcut is as the user left it

  @unreadable-choice-falls-back
  Scenario: A stored choice that cannot be read gives the default
    Given the installation's stored switcher shortcut cannot be read, or names a chord that cannot be the shortcut
    When the app launches
    Then the switcher shortcut is the platform's default

  @plain-browser-shortcut
  Scenario: A plain browser has the default chord only, and only on the app's own page
    Given the app open in a plain browser
    Then Settings offers no "Keyboard shortcuts"
    And the platform's default chord toggles the workspace switcher's menu while keyboard focus is on the app's own page
    And the chord does nothing in the app while keyboard focus is inside the workspace
