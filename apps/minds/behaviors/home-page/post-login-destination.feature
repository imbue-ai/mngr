Feature: Post-login destination
  When a user authenticates, "/post-login" decides where their browser lands, and the same first-run logic that governs "/" governs that choice.
  The decision is ordered.
  The one-time error-reporting consent screen precedes every other destination, so a user who has not answered it is sent to "/" regardless of any requested destination.
  A caller-supplied return destination is honored next, but only when it is a safe same-origin path -- "safe" is the no-open-redirects predicate, defined in browser-authorization/ and not restated here.
  Absent a usable return destination, the default depends on whether the user has any workspace yet: the account-management page if they do, the new-workspace form if they do not.
  The session gate on "/post-login", and the confinement of the return destination to the origin, are access-control facts specified in browser-authorization/.

  @consent-first
  Scenario: The unanswered consent question overrides every other destination
    Given an authenticated user who has not answered the consent question
    When they arrive at "/post-login", with or without a return destination
    Then they are redirected to "/", where the consent screen is shown

  @safe-return-to
  Scenario: A safe return destination wins
    Given an authenticated user who has answered the consent question
    When they arrive at "/post-login" with a return destination that is a path on this origin
    Then they are redirected to that path

  @default-destination
  Scenario Outline: Otherwise, the destination depends on whether any workspace exists
    Given an authenticated user who has answered the consent question
    And no return destination (or one that was rejected as unsafe)
    And they have <workspaces>
    When they arrive at "/post-login"
    Then they are redirected to <destination>

    Examples:
      | workspaces             | destination                       |
      | at least one workspace | the account-management page       |
      | no workspaces          | "/" (which shows the new-workspace form) |
