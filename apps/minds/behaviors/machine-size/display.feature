Feature: Showing a machine's size
  The workspace settings page always has a Machine size section.
  It shows the recorded size of a machine whose provider reports one, and otherwise says where the size stands: still being read, not reported by the provider, or not readable at all.

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
      | aws_<hex>    | a 30 GB disk   | 4 CPUs · 8 GB RAM · 30 GB disk   |

  @unknown-size-named
  Scenario: A machine whose provider reports no size has a Machine size section saying so
    Given a workspace whose machine's provider recorded no size
    When the user opens the workspace's General settings
    Then the Machine size section says the machine does not report its size

  @unread-size-named
  Scenario: A size the desktop client could not read is told apart from a machine without one
    Given a workspace whose machine's size the desktop client fails to read
    When the user opens the workspace's General settings
    Then the Machine size section says the machine's size could not be read
    And it does not say the machine does not report its size

  @imbue-cloud-size-shown-before-the-offer
  Scenario: An Imbue Cloud machine's size is shown before whether it can be changed is known
    An Imbue Cloud machine's size and whether the page may change it come from two connector reads, the second of which can take far longer than the first.
    Given an Imbue Cloud workspace whose machine has 8 GB of memory and a 28 GB disk
    When the user opens the workspace's General settings
    Then the Machine size section reads "8 GB RAM · 28 GB disk" as soon as the size is read
    And it says it is checking whether the size can be changed, in place of the size controls, until that is known

  @imbue-cloud-size-and-pending-change
  Scenario: An Imbue Cloud machine shows its memory and disk, and a pending change with the restart that applies it
    The vCPU count of an Imbue Cloud machine does not follow a resize, so it is not shown.
    Given an Imbue Cloud workspace whose machine has 8 GB of memory and a 28 GB disk, with 16 GB of memory pending
    When the user opens the workspace's General settings
    Then the Machine size section reads "8 GB RAM · 28 GB disk"
    And it notes that 16 GB RAM is pending and that restarting the machine applies it
