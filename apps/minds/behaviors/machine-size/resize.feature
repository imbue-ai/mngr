Feature: Changing a resizable machine's size
  The settings page changes the size of a local docker or lima machine through that provider's own resize, and restarts the machine so the new size takes effect.

  @resize-offered-only-where-possible
  Scenario Outline: Size fields and an apply action are offered only for a resizable machine
    Given a workspace whose machine's provider is "<provider>" and recorded a size
    When the user opens the workspace's General settings
    Then the Machine size section <offers>

    Examples:
      | provider          | offers                                                        |
      | docker            | offers fields for CPUs and memory and an "Apply and restart"  |
      | lima              | offers fields for CPUs, memory, and disk and an "Apply and restart" |
      | modal             | offers no size fields                                         |
      | imbue_cloud_alice | offers no size fields                                         |

  @resize-restarts-a-running-machine
  Scenario: Applying a new size to a running machine records it and restarts the machine
    Given a running docker workspace recorded at 4 CPUs and 8 GB of memory
    When the user types 16 as its memory and presses "Apply and restart"
    Then the docker provider is told to resize the machine's host to 16g of memory, and nothing else
    And the machine's restart is dispatched, to be followed like any restart
    And the page says the machine is restarting to apply the new size

  @resize-of-a-stopped-machine-waits
  Scenario: Applying a new size to a stopped machine records it for the machine's next start
    Given a stopped lima workspace
    When the user types 200 as its disk and presses "Apply and restart"
    Then the lima provider is told to grow the machine's data disk to 200 GiB
    And no restart is dispatched
    And the page says the new size applies when the machine next starts

  @unchanged-size-sends-nothing
  Scenario: Applying the size the machine already has sends nothing
    Given a running docker workspace recorded at 4 CPUs and 8 GB of memory
    When the user presses "Apply and restart" with the fields unchanged
    Then no resize is sent
    And the page asks for a new size

  @provider-refusal-is-quoted
  Scenario: A size the provider refuses is reported in the provider's own words
    Given a running docker workspace on a machine with 4 CPUs
    When the user types 64 as its CPUs and presses "Apply and restart"
    Then the page shows the provider's refusal
    And no restart is dispatched

  @unresizable-machines-refuse
  Scenario: A resize asked of a machine its provider cannot resize is refused
    Given a workspace whose machine's provider is "modal"
    When a resize is asked for it
    Then it is refused as a machine that cannot be resized after it was created
    And no provider is told anything

  @no-resize-under-another-operation
  Rule: No resize starts while another operation runs on the workspace
    A resize records the size before the restart claims the workspace's single operation slot, so the check happens first; a workspace with a running backup update, configure, restore, or recovery refuses the resize outright.
    Rationale: a restart under a running restore would tear the machine down mid-restore.
