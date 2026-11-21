Feature: Signing in on the first run
  The page shows one line, "Reimagine your personal computer", a Sign in button, a "Help improve Imbue Studio" checkbox, and a "Use without an account" link.
  Signing in is done in the browser; the page moves on as soon as an account lands.

  @sign-in-in-browser
  Scenario: Sign in opens the browser sign-in
    When the user presses "Sign in"
    Then the browser sign-in opens, where an account can be signed in to or created

  @reporting-answer
  Scenario Outline: The error-reporting checkbox is recorded when the user moves on
    Given the "Help improve Imbue Studio" checkbox, which starts checked, is <state>
    When the user presses "<way on>"
    Then error reporting is recorded as <recorded>

    Examples:
      | state     | way on                 | recorded |
      | checked   | Sign in                | allowed  |
      | unchecked | Sign in                | refused  |
      | checked   | Use without an account | allowed  |
      | unchecked | Use without an account | refused  |

  @reporting-explained
  Scenario: The checkbox's label says what it means without changing the answer
    When the user presses the label "Help improve Imbue Studio"
    Then what error reports contain, how long they are kept, and a link to the privacy policy are shown
    And the checkbox is as it was

  @sign-in-closed
  Scenario: Closing the sign-in without an account returns to the page
    Given the user pressed "Sign in"
    When the sign-in is closed before any account lands
    Then the page offers "Sign in" again

  @first-cloud-workspace
  Scenario: A new account's first cloud workspace starts at once
    Given the user pressed "Sign in"
    When the account "alice@example.com", named "Alice Smith", lands and has no workspaces
    Then the page greets "Welcome, Alice!"
    And a first cloud workspace is created under "alice@example.com" with no further question
    And the flow hands over to the creation page

  @existing-workspace-sign-in
  Scenario: Signing in to an account that has workspaces leaves for its home page
    Given the user pressed "Sign in"
    When an account that already has workspaces lands
    Then the app leaves the start flow for that account's home page
    And no workspace is created

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
    Then the page offers "Sign in" again, while signed-in accounts and verified emails persist
