Feature: Choosing a machine's size on the create form
  The advanced view of the create form takes a size for every compute mode that accepts one at create, in the terms that mode's provider accepts.

  Background:
    Given the create form is open on its advanced view

  @size-fields-for-numbered-modes
  Scenario Outline: A mode sized by numbers offers its dimensions, filled with its defaults
    When the user picks the "<mode>" compute mode
    Then the form offers a size field for each of <dimensions>
    And each field is filled with that mode's default

    Examples:
      | mode   | dimensions                   |
      | DOCKER | CPUs and memory              |
      | LIMA   | CPUs, memory, and data disk  |
      | MODAL  | CPUs and memory              |

  @no-size-fields-for-other-modes
  Scenario: A mode not sized by numbers offers no numeric size fields
    When the user picks the "IMBUE_CLOUD" compute mode
    Then the form offers no numeric size field
    And the create that goes out names no size

  @size-pick-kept-per-mode
  Scenario: A typed size belongs to the mode it was typed for
    Given the user picked the "DOCKER" compute mode and typed 16 as its memory
    When the user picks the "LIMA" compute mode
    Then the lima size fields show lima's defaults
    And picking the "DOCKER" compute mode again shows 16 as its memory

  @whole-numbers-only
  Scenario: A size that is not a whole number of at least 1 is refused before anything is sent
    Given the user picked the "DOCKER" compute mode
    When the user types "1.5" as its memory and submits the form
    Then the form names the size field as invalid
    And no create goes out

  @create-size-reaches-the-provider
  Scenario Outline: The chosen size reaches the provider in its own terms
    Given the user picked the "<mode>" compute mode and typed 4 CPUs and 16 GB of memory
    When the form is submitted
    Then the create tells the "<mode>" provider the size as <provider_terms>
    And the size follows the template's own defaults, so the user's value is the one the provider keeps

    Examples:
      | mode   | provider_terms                                             |
      | DOCKER | a CPU cap of 4 and a memory cap of 16g                     |
      | LIMA   | 4 CPUs and 16 GiB for the VM, and the data disk's size     |
      | MODAL  | a cpu of 4 and a memory of 16                              |

  @retry-restores-the-size
  Scenario: Retrying an interrupted create restores the size it was submitted with
    Given a create for the "LIMA" compute mode with 6 CPUs, 12 GB of memory, and a 250 GB disk was interrupted
    When the user opens the create form to retry it
    Then the lima size fields show 6, 12, and 250
