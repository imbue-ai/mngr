Feature: Signing in on the first run
  The page shows one line, "Reimagine your personal computer", a Sign in button, a "Help improve Studio" checkbox, and a "Use without an account" link.
  Signing in is done in the browser; the page moves on as soon as an account lands.
  Where it moves on to is decided by the reported workspace count: the account's home page when the count is greater than 0, a first cloud workspace when it is 0.
  The page waits on the record sync only for an account that landed without a reported workspace count.

  @sign-in-in-browser
  Scenario: Sign in opens the browser sign-in
    When the user presses "Sign in"
    Then the browser sign-in opens, where an account can be signed in to or created

  @reporting-answer
  Scenario Outline: The error-reporting checkbox is recorded when the user moves on
    Given the "Help improve Studio" checkbox, which starts checked, is <state>
    When the user presses "<way on>"
    Then error reporting is recorded as <recorded>

    Examples:
      | state     | way on                 | recorded |
      | checked   | Sign in                | allowed  |
      | unchecked | Sign in                | refused  |
      | checked   | Use without an account | allowed  |
      | unchecked | Use without an account | refused  |

  @reporting-explained
  Scenario: The checkbox says what it means on hover or focus, and its label toggles it
    When the user hovers over or focuses the "Help improve Studio" checkbox
    Then what error reports contain, how long they are kept, and a link to the privacy policy are shown
    And pressing the label "Help improve Studio" toggles the checkbox

  @without-account-explained
  Scenario: The way past sign-in says what an account adds
    When the user hovers over or focuses "Use without an account"
    Then the page says the workspace runs on this computer and that an account is needed to share it or use it from a phone

  @sign-in-closed
  Scenario: Closing the sign-in without an account returns to the page
    Given the user pressed "Sign in"
    When the sign-in is closed before any account lands
    Then the page offers "Sign in" again

  @first-cloud-workspace
  Scenario: An account with no workspaces gets its first cloud workspace at once
    Given the user pressed "Sign in"
    When the account "alice@example.com", named "Alice Smith", lands with a reported workspace count of 0
    Then the page greets "Welcome, Alice!"
    And a first cloud workspace is created under "alice@example.com" with no further question, without waiting on the record sync
    And the flow hands over to the creation page

  @existing-workspace-sign-in
  Scenario Outline: Signing in to an account that has workspaces leaves for its home page
    Given the user pressed "Sign in"
    When an account lands with a reported workspace count of <count>
    Then the app leaves the start flow for that account's home page, without waiting on the record sync
    And no workspace is created

    Examples:
      | count |
      | 1     |
      | 4     |

  @count-not-reported
  Scenario Outline: An account that lands without a reported workspace count is settled by the record sync
    Given the user pressed "Sign in"
    When the account "alice@example.com" lands without a reported workspace count
    And the record sync <record sync>
    Then <outcome>

    Examples:
      | record sync                              | outcome                                                      |
      | finds that the account has workspaces    | the app leaves the start flow for that account's home page   |
      | finds that the account has no workspaces | a first cloud workspace is created under "alice@example.com" |
      | fails                                    | a first cloud workspace is created under "alice@example.com" |
      | has not finished after 15 seconds        | a first cloud workspace is created under "alice@example.com" |

  @without-account
  Scenario: Using Studio without an account opens the create form
    When the user presses "Use without an account"
    Then the create form opens on running the workspace on this computer

  @refused-create
  Scenario: A refused first cloud workspace says why and offers to try again
    Given the landed account's first cloud workspace was refused
    Then the page says why it was refused
    And pressing "Try again" sends the create again under the same account

  @fresh-page
  Scenario: The page shown afresh starts over
    Given the user had pressed "Sign in"
    When the start flow is shown afresh
    Then the page offers "Sign in" again, while signed-in accounts persist
