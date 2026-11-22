Feature: Reaching and reading the Sharing pages
  Sharing is a page per share target under a workspace's Sharing entry, and every way of asking to share lands on one of those pages.
  Only the owner's side is here: a workspace shared with the account has no Sharing pages.

  @share-button-opens-sharing
  Scenario: The bar's Share opens Sharing on the whole workspace on screen
    Given a main window showing one of the account's own workspaces, "garden"
    When the user chooses Share in the bar
    Then Settings opens over "garden" on the Workspace & apps tab, on Sharing's "Full workspace" page, titled "Share garden"

  @share-button-with-settings-open
  Scenario Outline: With Settings already open, Share goes to the whole workspace's Sharing page, or closes it there
    Given a main window showing the account's workspace "garden", with Settings open on <place>
    When the user chooses Share in the bar
    Then <result>

    Examples:
      | place                                         | result                                                       |
      | the Studio tab                                | Settings shows "garden"'s Sharing, on "Full workspace"       |
      | another workspace's General page              | Settings shows "garden"'s Sharing, on "Full workspace"       |
      | the Sharing page of one of "garden"'s apps    | Settings shows "garden"'s Sharing, on "Full workspace"       |
      | "garden"'s Sharing, on "Full workspace"       | Settings closes                                              |

  @app-asks-to-share
  Scenario Outline: A workspace's own Share control opens Sharing on what it asked for
    Given a main window showing one of the account's own workspaces
    When the workspace asks to open sharing <request>
    Then Settings opens over that workspace on Sharing, on <page>
    And the workspace stays on screen behind Settings

    Examples:
      | request                                           | page                         |
      | for one of its apps                               | that app's page              |
      | naming no app                                     | the "Full workspace" page    |
      | for a name that is none of its share targets      | the "Full workspace" page    |

  @guest-cannot-open-sharing
  Scenario: A workspace shared with the account cannot open Sharing
    Given a main window showing a workspace another person shared with the account
    Then the bar offers no Share
    When that workspace asks to open sharing
    Then nothing opens

  @one-page-per-target
  Scenario: Each share target has a page of its own
    Given Settings is open on the Sharing of one of the account's own workspaces
    When the user chooses a target under the Sharing entry
    Then the whole workspace's page is titled "Share" followed by the workspace's name
    And an app's page is titled "Share the", the app's name, and "app"
    And it holds that target's own link with a code to scan, the people invited to that target, and the agents invited to it

  @sharing-page-does-not-wait-for-imbue-cloud
  Scenario: A Sharing page draws at once and fills in as each answer arrives
    Given Settings is opened on a Sharing page for the first time since the app started
    Then the page's title and every section are drawn at once
    And the link's place reads "Checking whether sharing is enabled" beside an empty box for the code
    When the workspace has answered what it holds, before Imbue Cloud has answered
    Then the people invited are listed and someone can be added
    And the link and its code are drawn if Imbue Cloud named the share's address earlier in this run of the app
    And the page says neither that sharing is on nor that it is off
    When Imbue Cloud has answered
    Then the switch shows whether sharing is on, and each person's row shows their invitation's status

  @sharing-switch-on-the-full-workspace-page
  Scenario: Sharing is turned on and off on the "Full workspace" page alone
    Given Settings is open on the Sharing of one of the account's own workspaces
    Then the "Enable sharing and web access" switch is the last thing on the "Full workspace" page
    And no app's page carries the switch

  @sharing-off-banner
  Scenario: While sharing is off, every Sharing page says so and offers to turn it on
    Given Settings is open on a Sharing page of a workspace whose sharing is off
    Then the page opens with "Sharing is off for this workspace." and a "Turn on sharing…" button
    And in the link's place it reads "The link appears here when sharing is on."
    When the user chooses "Turn on sharing…"
    Then the "Enable sharing?" confirmation opens, as the switch would open it

  @link-only-while-published
  Scenario: A target's link is shown only while the workspace is published
    Given Settings is open on a Sharing page of a published workspace whose link is ready
    Then the page shows the target's link, a control that copies it, and a code that encodes it
    And under the link it reads "This is a secure link: only people you've granted access can open it."
    When sharing is turned off
    Then the link, the copy control, and the code's pattern are gone from the page

  @mobile-link-per-target
  Scenario: Every Sharing page can email the account that page's own link
    Given Settings is open on a Sharing page of a published workspace whose link is ready
    When the user chooses "Email me a Mobile Access Link" on the "Full workspace" page
    Then the account is emailed the workspace's link, and the page reads "Sent to" with the account's address
    When the user chooses it on an app's page
    Then the account is emailed that app's own link, in a message that names the app inside its workspace
    And an app that has registered no link yet offers the control disabled, saying the app gets its link the first time it starts

  @access-list-folded
  Scenario: Who has access is folded away until asked for
    Given Settings is open on a Sharing page of a target that two people were invited to
    Then under "Invite others" a folded line reads that 2 have access to the target
    When the user unfolds it
    Then it lists each person invited to the target
    And a target nobody was invited to shows no list and nothing in its place

  @adding-someone-unfolds-the-list
  Scenario: Inviting someone unfolds who has access
    Given Settings is open on a Sharing page whose access list is folded
    When the user adds a person under "Invite others"
    Then the list unfolds, showing the person being added

  @invite-an-agent
  Scenario: Inviting an agent is a section of its own
    Given Settings is open on a Sharing page
    Then after "Invite others" a section headed "Invite an agent" offers "Create invite"
    And it lists the agents invited to this target, or reads "No agents have been invited."
    And the count of who has access covers people only

  @sharing-tiles-lead-on
  Scenario Outline: The reminders on a Sharing page open what they name
    Given Settings is open on <page> of one of the account's own workspaces
    When the user chooses <tile>
    Then Settings shows <destination>

    Examples:
      | page                         | tile                                       | destination                           |
      | Sharing's "Full workspace"   | an app under the apps invited users reach  | that app's Sharing page               |
      | any Sharing page             | one of the workspace's connections         | Permissions, at that connection       |
