Feature: Showing a machine's size
  The workspace settings page shows the recorded size of every machine whose provider reports one.

  @recorded-size-shown
  Scenario Outline: The settings page shows the recorded size for a machine on any provider
    Given a workspace whose machine's provider is "<provider>" and recorded 4 CPUs, 8 GB of memory, and <disk>
    When the user opens the workspace's General settings
    Then the Machine size section reads "<shown>"

    Examples:
      | provider     | disk           | shown                            |
      | docker       | no disk        | 4 CPUs · 8 GB RAM                |
      | lima         | a 100 GB disk  | 4 CPUs · 8 GB RAM · 100 GB disk  |
      | modal        | no disk        | 4 CPUs · 8 GB RAM                |
      | byok-aws     | a 30 GB disk   | 4 CPUs · 8 GB RAM · 30 GB disk   |

  @unknown-size-hidden
  Scenario: A machine whose provider reports no size has no Machine size section
    Given a workspace whose machine's provider recorded no size
    When the user opens the workspace's General settings
    Then no Machine size section is shown

  @imbue-cloud-size-and-pending-change
  Scenario: An Imbue Cloud machine shows its memory and disk, and a pending change with the restart that applies it
    The vCPU count of an Imbue Cloud machine does not follow a resize, so it is not shown.
    Given an Imbue Cloud workspace whose machine has 8 GB of memory and a 28 GB disk, with 16 GB of memory pending
    When the user opens the workspace's General settings
    Then the Machine size section reads "8 GB RAM · 28 GB disk"
    And it notes that 16 GB RAM is pending and that restarting the machine applies it
