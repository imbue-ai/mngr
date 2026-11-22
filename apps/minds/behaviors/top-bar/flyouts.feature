Feature: Flyouts
  Every surface opened from one of the bar's buttons behaves the same way, so each is one click from any other.

  @flyout-hangs-from-its-button
  Scenario Outline: A flyout hangs from its own button over a dimmed window
    Given a main window with no flyout open
    When the user chooses <button> in the bar
    Then <flyout> opens hanging from that button, joined to it
    And the rest of the window is dimmed, the bar included

    Examples:
      | button                  | flyout                 |
      | the account switcher    | the account menu       |
      | the workspace switcher  | the workspace menu     |
      | the notification bell   | the notification feed  |
      | Report a bug            | Get help               |
      | Settings                | Settings               |

  @flyout-dismissal
  Scenario Outline: A flyout closes on its own button, on Escape, or on a click on the dimmed window
    Given a flyout is open
    When the user <action>
    Then the flyout closes
    And the window shows what it showed before the flyout opened

    Examples:
      | action                                       |
      | chooses the flyout's own button again        |
      | presses Escape                               |
      | clicks the dimmed window outside the flyout  |

  @flyout-has-no-close-button
  Scenario: A flyout has no close button of its own
    Given a flyout is open
    Then the flyout shows no close button
    And a modal that is centred in the window instead of hung from a button shows one

  @bar-stays-live
  Scenario: The bar's other buttons stay live under an open flyout
    Given a main window with Get help open
    When the user chooses Settings in the bar
    Then Get help closes and Settings opens, in that one click
    And stepping back in the window's history does not bring Get help back

  @workspace-stays-behind
  Scenario: A flyout over a workspace leaves the workspace on screen
    Given a main window showing a workspace
    When the user opens a flyout
    Then the workspace stays on screen behind the dimmed window, and the trail still names it
    And closing the flyout returns to the workspace without loading it again

  @confirmation-over-settings
  Scenario: A confirmation opened from Settings is answered before Settings closes
    Given Settings is open, with a confirmation dialog open over it
    Then the dialog dims the whole window, the bar's buttons included
    When the user presses Escape
    Then the dialog closes and Settings stays open
    When the user presses Escape again
    Then Settings closes
