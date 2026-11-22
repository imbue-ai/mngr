Feature: Legacy addresses land on their place in Settings
  The account page and the workspace's options panel are gone, but their addresses were handed out in links and saved sessions.
  Each one still opens, on the place in Settings that holds what it showed.

  @legacy-address-lands
  Scenario Outline: A legacy address opens Settings on the place that replaced it
    Given an authenticated user who owns a workspace, written <id> in the addresses below
    When a window arrives at "<address>"
    Then it shows Settings on <place>
    And the window's address is Settings' own, with the legacy address gone from its history

    Examples:
      | address                                                   | place                                                              |
      | /accounts                                                 | the Accounts tab                                                   |
      | /workspace/<id>/options?tab=settings                      | that workspace's General page                                      |
      | /workspace/<id>/options?tab=settings&group=backup         | that workspace's Backup page                                       |
      | /workspace/<id>/options?tab=settings&group=updates        | that workspace's Updates page                                      |
      | /workspace/<id>/options?tab=settings&group=account        | that workspace's General page, scrolled to its Owner section       |
      | /workspace/<id>/options?tab=permissions                   | that workspace's Permissions                                       |
      | /workspace/<id>/options?tab=permissions&section=<section> | that workspace's Permissions, at that section                      |
      | /workspace/<id>/options?tab=share                         | that workspace's Sharing, on the "Full workspace" page             |
      | /workspace/<id>/options?tab=share&target=<app>            | that workspace's Sharing, on that app's page                       |
      | /workspace/<id>/options                                   | that workspace's Sharing, on the "Full workspace" page             |
      | /workspace/<id>/settings                                  | that workspace's General page                                      |
      | /sharing/<id>                                             | that workspace's Sharing, on the "Full workspace" page             |
      | /sharing/<id>/<app>                                       | that workspace's Sharing, on that app's page                       |

  @legacy-workspace-address-floats-over-it
  Scenario: A legacy workspace address opens Settings over that workspace
    Given an authenticated user and one of their own workspaces
    When a window arrives at that workspace's legacy options address
    Then Settings floats over that workspace, which is on screen behind it
    And closing Settings leaves the window on the workspace

  @legacy-accounts-address-keeps-its-account
  Scenario: The legacy accounts address keeps the window's account
    Given a window for the account "alice@example.com"
    When it arrives at "/accounts" naming Alice as the window's account
    Then Settings opens on the Accounts tab in a window still for Alice
