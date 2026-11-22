Feature: The About page of something shared with the account
  A guest has none of an owner's settings, so the Workspace & apps tab shows one page for a workspace or app shared with the account: whose it is, what of it the account can reach, and the way to take it off the account's lists.
  Taking it off the lists hides it from this account and changes nothing about the access its owner gave.

  @about-names-the-owner
  Scenario Outline: The page names the owner
    Given the pick is <pick>
    Then the "Owner" section reads <owner>

    Examples:
      | pick                                                                                    | owner                                                                |
      | a workspace shared by Sam Okafor, whose email is "sam@example.com"                      | "Sam Okafor", with "sam@example.com" under it                        |
      | a workspace shared by an owner known only by the email "sam@example.com"                | "sam@example.com" alone                                              |
      | a link the account pasted                                                               | "Unknown", with a line saying a pasted link names nobody             |

  @about-names-the-access
  Scenario Outline: The page says what of the workspace the account can reach
    Given the pick is <pick>
    Then the "Your access" section reads "<access>"
    And under it, that the owner decides what the account can reach there, and can change it

    Examples:
      | pick                                                                     | access                  |
      | a workspace shared whole with the account                                | The whole workspace     |
      | the apps "Recipes" and "Budget" of a workspace, shared with the account  | Only Recipes, Budget    |

  @about-removal-per-share
  Scenario Outline: The page's one action is named for what was shared
    Given the pick is <pick>
    Then the last section and its button read "<action>"
    And the section says <sentence>

    Examples:
      | pick                                                                            | action          | sentence                                                                                                                                                                        |
      | the workspace "harbor", shared by Sam Okafor                                    | Leave workspace | "Remove harbor from your workspaces. This hides it here; it does not change the access Sam Okafor gave you. You can undo it from Home › Invitations."                           |
      | the one app "Recipes" that Sam Okafor shared from a workspace                   | Remove app      | "Remove Recipes from your apps. This hides it here; it does not change the access Sam Okafor gave you. You can undo it from Home › Invitations."                                |
      | one of the apps "Recipes" and "Budget" that Sam Okafor shared from a workspace  | Remove apps     | "Remove everything Sam Okafor shared with you from this workspace (Recipes, Budget). This hides them here; it does not change the access Sam Okafor gave you. You can undo it from Home › Invitations." |
      | a link the account pasted                                                       | Remove link     | "Remove this link from your list."                                                                                                                                              |

  @removal-asks-first
  Scenario: The action asks before it removes anything
    Given the pick is the workspace "harbor", shared by Sam Okafor
    When the user chooses "Leave workspace"
    Then a confirmation titled "Leave harbor?" repeats what leaving does, with "Cancel" and "Leave"
    When the user chooses "Cancel"
    Then nothing is removed and the About page is still shown

  @leaving-hides-the-share
  Scenario: Leaving takes the share off this account's lists and returns the window home
    Given the pick is the workspace "harbor", shared by Sam Okafor
    When the user confirms "Leave workspace"
    Then Settings closes and the window shows the account's home page
    And "harbor" is no longer among the account's workspaces, in the workspace switcher, or in the picker
    And the share files under the home page's Invitations as declined, where it can be taken back

  @leaving-does-not-revoke
  Scenario: Leaving does not change the access the owner gave
    Given the user left the workspace "harbor", shared by Sam Okafor
    Then the access Sam gave the account is unchanged
    When the user takes the decline back from the home page's Invitations
    Then the share is an invitation the account can join again, without Sam sharing "harbor" again

  @removing-apps-removes-the-whole-share
  Scenario: Removing an app removes everything that share gave
    Given Sam Okafor shared the apps "Recipes" and "Budget" of one workspace with the account
    And the pick is "Recipes"
    When the user confirms "Remove apps"
    Then both "Recipes" and "Budget" leave the account's apps

  @removing-a-pasted-link-forgets-it
  Scenario: Removing a pasted link forgets the link
    Given the pick is a link the account pasted
    When the user confirms "Remove link"
    Then the link leaves the account's lists
    And nothing files under the home page's Invitations

  @removal-failure-stays
  Scenario: A removal that fails says why and removes nothing
    Given the pick is something shared with the account
    When the user confirms the removal and it fails
    Then the confirmation stays open with the failure's message
    And the share stays on the account's lists
