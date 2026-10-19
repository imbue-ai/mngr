Feature: Changing a resizable machine's size
  The settings page changes the size of a local docker or lima machine through that provider's own resize, and restarts the machine so the new size takes effect. For an Imbue Cloud machine it records the size on the connector within the plan's quotas, and offers the restart that applies it separately.

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
      | imbue_cloud_alice | offers no size fields when alice's plan is not offered the change |

  @imbue-cloud-sizes-offered-within-the-plan
  Scenario: An Imbue Cloud machine offers the RAM sizes its plan allows, a bounded disk, and no CPUs
    Given an Imbue Cloud workspace on an ally account whose plan allows 80 GB of RAM across running machines
    And the machine runs at 8 GB of memory with a 28 GB disk, beside another running 8 GB machine
    When the user opens the workspace's General settings
    Then the Machine size section offers the RAM sizes from 8 GB to 72 GB in steps of 8, the largest marked as the plan limit
    And it offers a disk field that grows from 28 GB up to what the plan's disk quota leaves
    And it offers no CPU field, saying CPUs scale with the RAM
    And it names what the plan allows and what the other running machines use

  @imbue-cloud-sizes-offered-to-the-ally-plan-only
  Scenario: An Imbue Cloud machine on any other plan shows its size read-only
    Given an Imbue Cloud workspace on an account whose plan is not ally
    When the user opens the workspace's General settings
    Then the Machine size section reads the machine's size
    And it offers no size fields

  @imbue-cloud-size-is-recorded-for-the-next-restart
  Scenario: Saving an Imbue Cloud size records it without restarting the machine
    Given a running Imbue Cloud workspace offered the sizes its plan allows
    When the user picks 16 GB of RAM and presses "Save size"
    Then the size is recorded on the connector for the machine, and nothing else
    And no restart is dispatched
    And the pending size is noted as applying when the machine restarts, with a "Restart now" that restarts the machine, saying the restart backs up the machine's disks first

  @imbue-cloud-quota-refusal-is-explained
  Scenario: A size the account's quota cannot hold is explained in the connector's terms
    Given a running Imbue Cloud workspace offered the sizes its plan allows
    When the recorded size is refused by the account's quota
    Then the page says the plan cannot hold this size right now, quoting the quota that refused it
    And no restart is dispatched

  @no-resize-while-the-machine-transitions
  Scenario: An Imbue Cloud machine that is starting or stopping holds its size controls
    Given an Imbue Cloud workspace offered the sizes its plan allows, whose machine is starting
    When the user opens the workspace's General settings
    Then the size controls are disabled
    And the page says the size can be changed once the machine finishes starting or stopping

  @restart-status-holds-the-controls
  Scenario: A restart dispatched from the page is the section's one status until the desktop client reports it over
    The connector keeps reporting a restarting machine as running with its size pending until the stop reaches it, so the section follows the desktop client's own record of the restart rather than the connector's.
    Given a running Imbue Cloud workspace offered the sizes its plan allows, with 16 GB of memory pending
    When the user presses "Restart now"
    Then the page says the machine is restarting to apply the new size, in place of the pending notice
    And no second restart is offered and the size controls are disabled
    And the size is re-read until the desktop client reports the restart over, after which the controls are offered again

  @restart-from-elsewhere-waited-out
  Scenario: A start or restart the desktop client runs with no size pending only holds the controls
    Given an Imbue Cloud workspace offered the sizes its plan allows, with no size pending
    And the desktop client is starting or restarting the machine from outside the settings page
    When the user opens the workspace's General settings
    Then the page says the machine is starting or restarting and that its size can be changed once that finishes
    And the size controls are disabled

  @resize-restarts-a-running-machine
  Scenario: Applying a new size to a running machine records it and restarts the machine
    Given a running docker workspace recorded at 4 CPUs and 8 GB of memory
    When the user types 16 as its memory and presses "Apply and restart"
    Then the docker provider is told to resize the machine's host to 16g of memory, and nothing else
    And the machine's restart is dispatched, to be followed like any restart
    And the page says the machine is restarting to apply the new size
    And no second resize is offered while it restarts

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
