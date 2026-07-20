Feature: Post-sign-in destination
  Account sign-in flows (out of scope here) all funnel through "/post-login",
  which decides where a just-signed-in user lands.

  @signed-out-arrival
  Scenario: Signed-out arrivals are sent to sign in
    Given the user is not signed in
    When they arrive at "/post-login"
    Then they are redirected toward sign-in, not to any destination

  @consent-first
  Scenario: The unanswered consent question overrides every other destination
    Given a signed-in user who has not answered the consent question
    When they arrive at "/post-login", with or without a return destination
    Then they are redirected to "/", where the consent screen is shown

  @safe-return-to
  Scenario: A safe return destination wins
    Given a signed-in user who has answered the consent question
    When they arrive at "/post-login" with a return destination that is a path on this origin
    Then they are redirected to that path

  @default-destination
  Scenario Outline: Otherwise, the destination depends on whether any workspace exists
    Given a signed-in user who has answered the consent question
    And no return destination (or one that was rejected as unsafe)
    And they have <workspaces>
    When they arrive at "/post-login"
    Then they are redirected to <destination>

    Examples:
      | workspaces             | destination                       |
      | at least one workspace | the account-management page       |
      | no workspaces          | "/" (which shows the create form) |
